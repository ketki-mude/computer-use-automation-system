"""Deterministic replay: run a capability with inputs, no LLM anywhere in the loop.

For each step: wait until its target resolves to exactly one element (or a known screen
appears), check the policy, act, then wait for the step's postcondition. Known screens
are classified by the capability's outcomes and the app profile:

  business     -> return a business outcome to the caller (not an error)
  recoverable  -> handle it here (dismiss a notice, sign on again) and carry on
  failure      -> stop with a debuggable error and evidence
  escalate     -> hand the live session to a person

A screen that matches nothing known fails closed. After an irreversible step has run,
nothing is ever retried automatically.
"""

import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from playwright.async_api import Error as PlaywrightError

from .evidence import RunLog
from .executor import Detected, Executor, SignOnFailed
from .handoff import Handoff, HandoffResolution
from .schema import (
    RISK_ORDER,
    AppProfile,
    BusinessOutcome,
    Capability,
    Condition,
    Drift,
    ErrorKind,
    OutputSpec,
    Recovery,
    RunError,
    RunResult,
    Step,
)
from .surface.web import ApprovalRequired, NotInControl, PolicyViolation, WebSurface

MAX_RECOVERIES_PER_STEP = 3
MAX_RESTARTS = 1


@dataclass
class StepFailed(Exception):
    kind: ErrorKind
    expected: str
    observed: str
    retryable: bool = False


def validate_inputs(cap: Capability, params: dict[str, str]) -> list[str]:
    problems = []
    for name, spec in cap.inputs.items():
        if name not in params:
            problems.append(f"missing input {name}")
            continue
        value = params[name]
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            problems.append(f"{name} does not match {spec.pattern}")
        if spec.type == "integer" and not re.fullmatch(r"-?\d+", value):
            problems.append(f"{name} must be an integer")
        if spec.type == "decimal":
            try:
                Decimal(value)
            except InvalidOperation:
                problems.append(f"{name} must be a decimal number")
    problems += [f"unknown input {n}" for n in params if n not in cap.inputs]
    return problems


def parse_output(text: str, spec: OutputSpec) -> str | int:
    """Money stays a string ("1204.50") so it never passes through a float."""
    raw = text.strip()
    if spec.parse == "currency" or spec.type == "decimal":
        cleaned = raw.replace("$", "").replace(",", "").strip()
        negative = cleaned.startswith("(") and cleaned.endswith(")")
        value = Decimal(cleaned.strip("()"))
        return str(-value if negative else value)
    if spec.parse == "integer" or spec.type == "integer":
        return int(raw.replace(",", ""))
    return raw


class ReplayEngine:
    def __init__(self, surface: WebSurface, profile: AppProfile, log: RunLog,
                 handoff: Handoff | None = None):
        self.surface = surface
        self.profile = profile
        self.log = log
        self.handoff = handoff

    async def run(self, cap: Capability, params: dict[str, str], *, allow_irreversible: bool = False,
                  stop_before_irreversible: bool = False) -> RunResult:
        started = time.monotonic()
        result = RunResult(status="failed", capability=cap.ref, run_id=self.log.run_id,
                           evidence_dir=str(self.log.dir))
        self._result = result
        self._cap = cap
        self._params = params
        self._allow_irreversible = allow_irreversible
        self._committed = False  # an irreversible step has run
        self._current_step_id: str | None = None
        self._step_acted = False
        for name, spec in cap.inputs.items():
            self.log.redactor.register(params.get(name), spec.sensitivity)
        self.log.event("replay_started", f"{cap.ref} with inputs "
                       + ", ".join(f"{k}={v!r}" for k, v in params.items()),
                       capability=cap.ref, inputs=params)
        try:
            if problems := validate_inputs(cap, params):
                raise StepFailed("INPUT_INVALID", "inputs matching the capability's schema",
                                 "; ".join(problems))
            if RISK_ORDER[cap.risk] == 2 and not (allow_irreversible or stop_before_irreversible):
                raise StepFailed("APPROVAL_REQUIRED", "explicit approval for an irreversible capability",
                                 "replay was not started with --allow-irreversible")
            self.executor = Executor(self.surface, self.profile, self.log, outcomes=cap.outcomes)
            await self._sign_on()
            await self.surface.start_trace()
            if await self._run_steps(stop_before_irreversible):
                await self._check_success()
            # else: a validation run stopped before the first irreversible step; everything
            # up to the commit point replayed, and the checkpoint lies beyond it.
            result.status = "success"
        except StepFailed as e:
            await self._fail(e)
        except _Finished:
            pass
        result.duration_ms = int((time.monotonic() - started) * 1000)
        keep_trace = result.status not in ("success", "business_outcome")
        await self.surface.stop_trace(self.log.path("trace.zip") if keep_trace else None)
        self.log.event(f"finished_{result.status}", self._summary(), status=result.status)
        self.log.write_json("result.json", result.model_dump(mode="json"))
        return result

    # ------------------------------------------------------------ steps

    async def _sign_on(self) -> None:
        try:
            interrupted = await self.executor.sign_on()
        except SignOnFailed as e:
            raise StepFailed("SURFACE_ERROR", "the signed-in home screen", str(e)) from e
        if interrupted:
            await self._on_detected(interrupted, None)
            if not (await self.executor.holds(self.profile.signed_in, {}))[0]:
                raise StepFailed("UNKNOWN_STATE", "the signed-in home screen",
                                 await self._observed())
        self.log.event("signed_in", f"app version {await self.executor.app_version()}")

    async def _run_steps(self, stop_before_irreversible: bool) -> bool:
        steps = self._cap.steps
        i, restarts, recoveries = 0, 0, 0
        while i < len(steps):
            step = steps[i]
            self._current_step_id, self._step_acted = step.id, False
            if stop_before_irreversible and step.risk == "irreversible":
                self.log.event("stopped_before_commit", f"stopping before irreversible step {step.id}")
                return False
            outcome = await self._run_step(step)
            if outcome is None:
                i, recoveries = i + 1, 0
                continue
            action = await self._on_detected(outcome, step)
            recoveries += 1
            if recoveries > MAX_RECOVERIES_PER_STEP:
                raise StepFailed("UNKNOWN_STATE", f"step {step.id} to complete",
                                 f"{outcome.code} kept reappearing")
            if action == "restart":
                restarts += 1
                if restarts > MAX_RESTARTS:
                    raise StepFailed("UNKNOWN_STATE", "a clean run after restarting",
                                     f"{outcome.code} happened again after a restart")
                i = 0
            elif action == "resume_at":
                i = self._resume_index
            elif self._step_acted:
                # The screen appeared after this step already acted: the step is done.
                # Running it again could repeat an irreversible action, so move on.
                i, recoveries = i + 1, 0
        return True

    async def _run_step(self, step: Step) -> Detected | None:
        """Run one step. Returns a Detected when a known screen interrupts it."""
        res = None

        async def found() -> bool:
            nonlocal res
            res = await self.surface.resolve(step.target, self._params)
            return res.found

        waited = await self.executor.wait_until(found, self._params, step.timeout_s)
        if isinstance(waited, Detected):
            return waited
        if waited == "timeout":
            kind = "TARGET_AMBIGUOUS" if res and res.ambiguous else "TARGET_NOT_FOUND"
            tried = ", ".join(f"{k}:{n}" for k, n in (res.attempts if res else []))
            raise StepFailed(kind, f"step {step.id}: {_target_text(step)} (tried {tried})",
                             await self._observed(), retryable=not self._committed)
        if res.strategy_index > 0:
            note = f"primary locator failed; matched by fallback {res.strategy_index} ({step.target.strategies[res.strategy_index].kind})"
            self._result.drift.append(Drift(step_id=step.id, strategy_used=res.strategy_index, note=note))
            self.log.event("drift", f"{step.id}: {note}", step_id=step.id)
        fp = step.target.fingerprint
        if fp and res.element and (fp.role != res.element.get("role") or fp.tag != res.element.get("tag")):
            if step.risk == "irreversible":
                raise StepFailed("TARGET_NOT_FOUND", f"{fp.role} <{fp.tag}> for irreversible step {step.id}",
                                 f"{res.element.get('role')} <{res.element.get('tag')}>")
            self._result.drift.append(Drift(step_id=step.id, strategy_used=res.strategy_index,
                                            note="element role or tag differs from discovery"))

        value = self.executor.value(step.value, self._params) if step.value is not None else None
        try:
            acted = await self.surface.act(res, step.action, value,
                                           allow_irreversible=self._allow_irreversible)
        except PolicyViolation as e:
            raise StepFailed("POLICY_BLOCKED", f"step {step.id} within the allowlist", str(e)) from e
        except ApprovalRequired as e:
            raise StepFailed("APPROVAL_REQUIRED", f"approval for step {step.id}", str(e)) from e
        except NotInControl as e:
            raise StepFailed("ESCALATION_UNRESOLVED", "automation holding the session", str(e)) from e
        except PlaywrightError as e:
            raise StepFailed("SURFACE_ERROR", f"step {step.id}: {step.action} to succeed",
                             str(e).splitlines()[0], retryable=not self._committed) from e
        self._step_acted = True
        if acted.risk == "irreversible":
            self._committed = True
        shown = ""
        if step.action == "extract":
            spec = self._cap.outputs[step.output]
            try:
                parsed = parse_output(acted.text or "", spec)
            except (InvalidOperation, ValueError) as e:
                raise StepFailed("CHECKPOINT_FAILED", f"a {spec.type} for {step.output}",
                                 repr(acted.text)) from e
            self._result.outputs[step.output] = parsed
            self.log.redactor.register(str(parsed), spec.sensitivity)
            self.log.redactor.register(acted.text, spec.sensitivity)
            shown = f" -> {step.output} = {parsed}"
        self.log.event("step_done", f"{step.id}: {step.intent}{shown}", step_id=step.id,
                       strategy=res.strategy_index, risk=acted.risk)

        if step.expect:
            cond = step.expect.model_dump()

            async def expected() -> bool:
                return (await self.executor.holds(_condition(cond), self._params))[0]

            waited = await self.executor.wait_until(expected, self._params, step.timeout_s)
            if isinstance(waited, Detected):
                return waited
            if waited == "timeout":
                raise StepFailed("CHECKPOINT_FAILED", f"after {step.id}: {_expect_text(step)}",
                                 await self._observed(), retryable=not self._committed)
        return None

    async def _check_success(self) -> None:
        ck = self._cap.success
        if ck.title_contains or ck.text:
            cond = _condition({"title_contains": ck.title_contains, "text": ck.text, "frame": ck.frame})

            async def holds() -> bool:
                return (await self.executor.holds(cond, self._params))[0]

            waited = await self.executor.wait_until(holds, self._params, 10)
            if isinstance(waited, Detected):
                await self._on_detected(waited, None)
                raise StepFailed("CHECKPOINT_FAILED", "the success checkpoint", waited.message)
            if waited == "timeout":
                raise StepFailed("CHECKPOINT_FAILED", f"success checkpoint: {ck.model_dump(exclude_none=True)}",
                                 await self._observed())
        if ck.outputs_present and (missing := set(self._cap.outputs) - set(self._result.outputs)):
            raise StepFailed("CHECKPOINT_FAILED", "every declared output", f"missing {sorted(missing)}")
        self.log.event("checkpoint", "success checkpoint passed")

    # ------------------------------------------------------------ known screens

    async def _on_detected(self, d: Detected, step: Step | None) -> str:
        """Act on a known screen. Returns "retry" (same step), "restart" or "resume_at",
        or raises/finishes the run."""
        sid = step.id if step else "sign-on"
        self.log.event("detector", f"{d.code} ({d.rule.kind}): {d.message}", code=d.code,
                       kind=d.rule.kind, step_id=sid)
        kind = d.rule.kind
        if kind == "business":
            self._result.status = "business_outcome"
            self._result.outcome = BusinessOutcome(code=d.code, message=d.message, step_id=sid)
            raise _Finished()
        if kind == "failure":
            raise StepFailed("APP_ERROR", f"step {sid} to proceed" if sid else "the app to respond",
                             f"{d.code}: {d.message}", retryable=not self._committed)
        if kind == "escalate":
            return await self._escalate(d.code, d.message, step)
        # recoverable
        recovery = d.rule.recovery
        if recovery.action == "click":
            res = await self.surface.resolve(recovery.target, self._params)
            if not res.found:
                raise StepFailed("UNKNOWN_STATE", f"the recovery control for {d.code}", await self._observed())
            await self.surface.act(res, "click")
            self._result.recoveries.append(Recovery(step_id=sid, condition=d.code, action="clicked"))
            self.log.event("recovery", f"{d.code}: clicked {res.element.get('name')!r}, retrying {sid}")
            return "retry"
        if recovery.action == "relogin_restart":
            if self._committed:
                return await self._escalate(d.code, "session expired after an irreversible step; "
                                            "a restart could repeat it", step)
            self.log.event("recovery", f"{d.code}: signing on again and restarting from step 1 "
                           "(every completed step was safe to repeat)")
            await self._sign_on()
            self._result.recoveries.append(Recovery(step_id=sid, condition=d.code,
                                                    action="relogin_and_restart"))
            return "restart"
        raise StepFailed("UNKNOWN_STATE", f"a recovery for {d.code}", recovery.action)

    async def _escalate(self, code: str, reason: str, step: Step | None) -> str:
        if self.handoff is None:
            raise StepFailed("ESCALATION_UNRESOLVED", "a human operator", f"{code}: {reason} "
                             "(no operator console in this run; start replay with --operator)")
        shot = await self.surface.screenshot(self.log.path("intervention.png"),
                                             self.log.redactor.sensitive_values())
        resolution: HandoffResolution = await self.handoff.request(
            run_id=self.log.run_id, capability=self._cap.ref, step_id=step.id if step else "sign-on",
            reason=f"{code}: {reason}", screenshot=shot)
        self._result.handoffs.append(resolution.record)
        if resolution.record.resolution == "aborted":
            raise StepFailed("ESCALATION_UNRESOLVED", "an operator to resolve the intervention",
                             "the operator aborted the run")
        if resolution.record.resolution == "timed_out":
            self._result.status = "escalated"
            self._result.error = RunError(kind="ESCALATION_UNRESOLVED", step_id=step.id if step else None,
                                          expected="an operator response", observed="no response in time")
            raise _Finished()
        if resolution.record.resolution == "completed_by_human":
            raise StepFailed("ESCALATION_UNRESOLVED", "outputs read by automation",
                             "the operator completed the task by hand; outputs were not captured")
        # resumed: find where to continue from what is on screen now
        self._resume_index = await self._find_resume_point(step)
        return "resume_at"

    async def _find_resume_point(self, step: Step | None) -> int:
        start = self._cap.steps.index(step) if step else 0
        for i in range(start, len(self._cap.steps)):
            if (await self.surface.resolve(self._cap.steps[i].target, self._params)).found:
                self.log.event("resumed", f"resuming at step {self._cap.steps[i].id}")
                return i
        if (await self.executor.holds(self.profile.signed_in, {}))[0]:
            self.log.event("resumed", "resuming from step 1")
            return 0
        raise StepFailed("UNKNOWN_STATE", "a screen where a step can resume", await self._observed())

    # ------------------------------------------------------------ evidence

    async def _fail(self, e: StepFailed) -> None:
        shot = None
        dom = None
        if self.surface.page is not None and e.kind not in ("INPUT_INVALID", "APPROVAL_REQUIRED"):
            shot = await self.surface.screenshot(self.log.path("failure.png"),
                                                 self.log.redactor.sensitive_values())
            dom = self.log.write_text("dom_snapshot.html", await self.surface.dom_snapshot())
        self._result.status = "failed"
        self._result.error = RunError(kind=e.kind, step_id=self._current_step_id, expected=e.expected,
                                      observed=self.log.redactor.text(e.observed),
                                      retryable=e.retryable and not self._committed,
                                      screenshot=str(shot) if shot else None,
                                      dom_snapshot=str(dom) if dom else None)

    async def _observed(self) -> str:
        """A short description of what is on screen, for error messages."""
        try:
            screens = await self.surface.screens()
        except PlaywrightError:
            return "the page could not be read"
        main = screens.get("main") or screens.get("top") or next(iter(screens.values()), None)
        if not main:
            return "no page"
        text = re.sub(r"\s+", " ", main["text"]).strip()
        return f'title "{main["title"]}": {text[:240]}'

    def _summary(self) -> str:
        r = self._result
        if r.status == "success":
            return "outputs " + ", ".join(f"{k}={v}" for k, v in r.outputs.items())
        if r.status == "business_outcome":
            return f"{r.outcome.code}: {r.outcome.message}"
        if r.error:
            return f"{r.error.kind}: expected {r.error.expected}; observed {r.error.observed}"
        return r.status


class _Finished(Exception):
    """The run reached a final answer early (business outcome or unresolved escalation)."""


def _condition(d: dict) -> Condition:
    return Condition(**{k: v for k, v in d.items() if v is not None})


def _target_text(step: Step) -> str:
    s = step.target.strategies[0]
    return f"{s.kind} {s.model_dump(exclude={'kind'})}"


def _expect_text(step: Step) -> str:
    return ", ".join(f"{k} {v!r}" for k, v in step.expect.model_dump(exclude_none=True).items())
