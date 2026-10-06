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

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatch

from ..evidence_writer import RunLog
from ..human_handoff import ticket_instructions
from ..human_handoff.control_lease import Handoff, HandoffResolution, TicketKind
from ..models.app_profile import AppProfile
from ..models.capability import RISK_ORDER, Capability, Condition, OutputSpec, Step
from ..models.run_result import BusinessOutcome, Drift, ErrorKind, Recovery, RunError, RunResult
from ..safety.log_masking import register_personal_data
from ..screen.screen_interface import (
    ApprovalRequired,
    NotInControl,
    PolicyViolation,
    Screen,
    ScreenError,
)
from ..settings import TRANSIENT_RETRY_BACKOFF_S
from .known_screens import Detected, KnownScreens
from .sign_on import SignOnFailed, read_app_version, resolve_step_value, sign_on

MAX_RECOVERIES_PER_STEP = 3
MAX_RESTARTS = 1
MAX_STUCK_TICKETS = 2
# Failures a person can often fix on the live screen (a pop-up, a wrong page).
STUCK_KINDS = {"TARGET_NOT_FOUND", "TARGET_AMBIGUOUS", "CHECKPOINT_FAILED", "UNKNOWN_STATE",
               "SURFACE_ERROR"}


@dataclass
class StepFailed(Exception):
    kind: ErrorKind
    expected: str
    observed: str
    retryable: bool = False
    plain: str | None = None  # what happened in words for a person, when the kind alone is too vague


def validate_inputs(cap: Capability, params: dict[str, str]) -> list[str]:
    problems = []
    for name, spec in cap.inputs.items():
        if name not in params:
            problems.append(f"missing input {name}")
            continue
        value = params[name]
        # re.ASCII: \d must mean 0-9, not every Unicode digit (e.g. Arabic-Indic numerals).
        if spec.pattern and not re.fullmatch(spec.pattern, value, flags=re.ASCII):
            problems.append(f"{name} does not match {spec.pattern}")
        if spec.type == "integer" and not re.fullmatch(r"-?[0-9]+", value):
            problems.append(f"{name} must be an integer")
        # A plain number only: Decimal() alone would also accept "NaN", "Infinity" and "1e3".
        if spec.type == "decimal" and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
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


class ReplayRunner:
    """Runs one capability with inputs, no LLM, and builds the RunResult."""

    def __init__(self, screen: Screen, profile: AppProfile, log: RunLog,
                 handoff: Handoff | None = None):
        self.screen = screen
        self.profile = profile
        self.log = log
        self.handoff = handoff

    async def run(self, cap: Capability, params: dict[str, str], *, allow_irreversible: bool = False,
                  stop_before_irreversible: bool = False) -> RunResult:
        started = time.monotonic()
        result = RunResult(status="failed", capability=cap.ref, run_id=self.log.run_id,
                           evidence_dir=self.log.display_dir())
        self._result = result
        self._cap = cap
        self._params = params
        self._allow_irreversible = allow_irreversible
        self._committed = False  # an irreversible step has run
        self._approved_step: str | None = None  # a person approved this irreversible step, once
        self._current_step_id: str | None = None
        self._current_index = 0
        self._step_acted = False
        self._dialogs_seen = len(self.screen.dialogs)
        for name, spec in cap.inputs.items():
            self.log.redactor.register(params.get(name), spec.sensitivity)
        for name in params.keys() - cap.inputs.keys():  # undeclared: no sensitivity known, so mask it
            self.log.redactor.register(params[name], "pii")
        self.log.event("replay_started", f"{cap.ref} with inputs "
                       + ", ".join(f"{k}={v!r}" for k, v in params.items()),
                       capability=cap.ref, inputs=params)
        try:
            if problems := validate_inputs(cap, params):
                raise StepFailed("INPUT_INVALID", "inputs matching the capability's schema",
                                 "; ".join(problems))
            if RISK_ORDER[cap.risk] == 2 and not (allow_irreversible or stop_before_irreversible
                                                  or self.handoff):
                raise StepFailed("APPROVAL_REQUIRED", "a person to approve the irreversible step",
                                 "no operator is connected and --allow-irreversible was not given")
            self.known = KnownScreens(self.screen, self.profile, self.log, outcomes=cap.outcomes)
            await self._sign_on()
            await self.screen.start_trace()
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
        await self.screen.stop_trace(self.log.path("trace.zip") if keep_trace else None)
        self.log.event(f"finished_{result.status}", self._summary(), status=result.status)
        self.log.write_json("result.json", result.model_dump(mode="json"))
        return result

    # ------------------------------------------------------------ steps

    async def _sign_on(self) -> None:
        try:
            interrupted = await sign_on(self.known)
        except SignOnFailed as e:
            raise StepFailed("SURFACE_ERROR", "the signed-in home screen", str(e)) from e
        if interrupted:
            await self._on_detected(interrupted, None)
            if not (await self.known.holds(self.profile.signed_in, {}))[0]:
                raise StepFailed("UNKNOWN_STATE", "the signed-in home screen",
                                 await self._observed())
        version = await read_app_version(self.known)
        self.log.event("signed_in", f"app version {version}")
        if version and not fnmatch(version, self._cap.app.versions):
            # Still try: most releases keep most screens. The record says why steps may drift.
            note = f"app version {version} is outside the versions this capability was proven on ({self._cap.app.versions})"
            self._result.drift.append(Drift(step_id="sign-on", strategy_used=0, note=note))
            self.log.event("drift", note)

    async def _run_steps(self, stop_before_irreversible: bool) -> bool:
        steps = self._cap.steps
        i, restarts, recoveries, stuck_tickets = 0, 0, 0, 0
        while i < len(steps):
            step = steps[i]
            if self.handoff is not None and (request := self.handoff.pop_request()):
                i, recoveries = await self._staff_request(step, *request), 0
                continue
            self._current_step_id, self._step_acted, self._current_index = step.id, False, i
            if stop_before_irreversible and step.risk == "irreversible":
                self.log.event("stopped_before_commit", f"stopping before irreversible step {step.id}")
                return False
            try:
                outcome = await self._run_step(step)
            except StepFailed as e:
                # Something a person can likely fix on the live screen: raise a ticket instead
                # of failing, if an operator is connected.
                if e.kind not in STUCK_KINDS or self.handoff is None or stuck_tickets >= MAX_STUCK_TICKETS:
                    raise
                stuck_tickets += 1
                i, recoveries = await self._stuck(step, e), 0
                continue
            if isinstance(outcome, _Jump):
                i, recoveries = outcome.index, 0
                continue
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

    async def _run_step(self, step: Step) -> "Detected | _Jump | None":
        """Run one step. Returns a Detected when a known screen interrupts it, or a _Jump
        when a person performed the step or approved it and the run continues elsewhere."""
        # An approval covers this one attempt at this one step: whatever happens next (a
        # failure, a restart, another ticket), a later attempt asks again.
        approved, self._approved_step = self._approved_step == step.id, None
        res = None

        async def found() -> bool:
            nonlocal res
            res = await self.screen.resolve(step.target, self._params)
            return res.found

        waited = await self.known.wait_until(found, self._params, step.timeout_s)
        self._judge_dialogs(step)
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
        # The fingerprint is the element's DOM tag and role; a pixel surface has neither.
        on_pixels = bool(res.element and res.element.get("surface") == "pixel")
        if fp and res.element and not on_pixels and (fp.role != res.element.get("role")
                                                      or fp.tag != res.element.get("tag")):
            if step.risk == "irreversible":
                raise StepFailed("TARGET_NOT_FOUND", f"{fp.role} <{fp.tag}> for irreversible step {step.id}",
                                 f"{res.element.get('role')} <{res.element.get('tag')}>")
            self._result.drift.append(Drift(step_id=step.id, strategy_used=res.strategy_index,
                                            note="element role or tag differs from discovery"))

        allow_irreversible = self._allow_irreversible or approved
        if step.risk == "irreversible" and not allow_irreversible:
            return await self._human_commit(step, res)

        value = resolve_step_value(step.value, self._params, self.profile, self.log.redactor) if step.value is not None else None
        try:
            acted = await self.screen.act(res, step.action, value, allow_irreversible=allow_irreversible)
        except PolicyViolation as e:
            raise StepFailed("POLICY_BLOCKED", f"step {step.id} within the allowlist", str(e)) from e
        except ApprovalRequired as e:
            raise StepFailed("APPROVAL_REQUIRED", f"approval for step {step.id}", str(e)) from e
        except NotInControl as e:
            raise StepFailed("ESCALATION_UNRESOLVED", "automation holding the session", str(e)) from e
        except ScreenError as e:
            raise StepFailed("SURFACE_ERROR", f"step {step.id}: {step.action} to succeed",
                             str(e).splitlines()[0], retryable=not self._committed) from e
        self._step_acted = True
        if acted.risk == "irreversible":
            self._committed = True
        self._judge_dialogs(step)
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
                return (await self.known.holds(_condition(cond), self._params))[0]

            waited = await self.known.wait_until(expected, self._params, step.timeout_s)
            self._judge_dialogs(step)
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
                return (await self.known.holds(cond, self._params))[0]

            waited = await self.known.wait_until(holds, self._params, 10)
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
            res = await self.screen.resolve(recovery.target, self._params)
            if not res.found:
                raise StepFailed("UNKNOWN_STATE", f"the recovery control for {d.code}", await self._observed())
            await self.screen.act(res, "click")
            self._result.recoveries.append(Recovery(step_id=sid, condition=d.code, action="clicked"))
            self.log.event("recovery", f"{d.code}: clicked {res.element.get('name')!r}, retrying {sid}")
            return "retry"
        if recovery.action == "retry_previous":
            return await self._retry_previous(d, step)
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

    def _judge_dialogs(self, step: Step) -> None:
        """Native dialogs were answered as they appeared (see Screen._on_dialog). Known
        ones and plain alerts are recorded as recoveries; an unexpected question was cancelled
        for safety, which may have changed what the app does, so the step stops."""
        new = self.screen.dialogs[self._dialogs_seen:]
        self._dialogs_seen = len(self.screen.dialogs)
        for d in new:
            if d["code"] or d["type"] == "alert":
                self._result.recoveries.append(Recovery(
                    step_id=step.id, condition=d["code"] or "UNEXPECTED_ALERT",
                    action=f"{d['action']}ed {d['type']} {d['message'][:60]!r}"))
            else:
                raise StepFailed("UNKNOWN_STATE", f"step {step.id} without an unexpected {d['type']} dialog",
                                 f"the app asked {d['message']!r}; it was cancelled for safety",
                                 plain=f"The app asked \"{d['message']}\", and it was answered 'Cancel' to be safe")

    async def _retry_previous(self, d: Detected, step: Step | None) -> str:
        """A transient page error: go back and redo the navigation that failed, if that is safe."""
        if step is None:  # during sign-on: the failed page is the landing page, safe to reload
            await self.screen.reload("main")
            self._result.recoveries.append(Recovery(step_id="sign-on", condition=d.code, action="reloaded"))
            return "retry"
        failed_at = self._current_index if self._step_acted else max(self._current_index - 1, 0)
        culprit = self._cap.steps[failed_at]
        if self._committed or culprit.risk == "irreversible":
            return await self._escalate(d.code, "a page failed after an irreversible step; retrying "
                                        "could repeat it", step)
        await asyncio.sleep(TRANSIENT_RETRY_BACKOFF_S)
        await self.screen.go_back(culprit.target.frame or "main")
        self._resume_index = await self._find_resume_point(self._cap.steps[max(failed_at - 1, 0)])
        self._result.recoveries.append(Recovery(
            step_id=step.id, condition=d.code,
            action=f"went back and retried from {self._cap.steps[self._resume_index].id}"))
        self.log.event("recovery", f"{d.code}: went back and retried from "
                       f"{self._cap.steps[self._resume_index].id}")
        return "resume_at"

    # ------------------------------------------------------------ tickets

    async def _register_screen_pii(self) -> None:
        with contextlib.suppress(ScreenError):
            register_personal_data(await self.screen.observe(), self.profile.pii_fields,
                                   self.log.redactor)

    async def _ticket(self, kind: TicketKind, title: str, reason: str, steps: list[str],
                      step: Step | None) -> HandoffResolution:
        """Raise a ticket and wait. Shared ending: aborted fails, unanswered escalates."""
        self._approved_step = None  # a new ticket means the screen may change: approve again
        await self._register_screen_pii()
        shot = await self.screen.screenshot(self.log.path(f"ticket-{len(self._result.handoffs) + 1}.png"),
                                             self.log.redactor.sensitive_values())
        context = {k.replace("_", " "): v for k, v in self._params.items()}
        if hint := await self._session_hint():
            context["session"] = hint
        if self.screen.debug_port:
            context["_cdp"] = f"http://127.0.0.1:{self.screen.debug_port}"
        resolution: HandoffResolution = await self.handoff.request(
            run_id=self.log.run_id, capability=self._cap.ref, step_id=step.id if step else "sign-on",
            kind=kind, title=title, reason=reason, instructions=steps, context=context,
            screenshot=shot)
        self._result.handoffs.append(resolution.record)
        outcome = resolution.record.resolution
        if outcome == "aborted":
            raise StepFailed("ESCALATION_UNRESOLVED", "an operator to resolve the ticket",
                             "the operator aborted the run")
        if outcome == "timed_out":
            self._result.status = "escalated"
            self._result.error = RunError(kind="ESCALATION_UNRESOLVED", step_id=step.id if step else None,
                                          expected="an operator to take the ticket",
                                          observed="nobody answered in time")
            raise _Finished()
        if outcome == "completed_by_human":
            self._result.status = "escalated"
            self._result.error = RunError(kind="ESCALATION_UNRESOLVED", step_id=step.id if step else None,
                                          expected="automation to finish and read the outputs",
                                          observed="a person finished the task by hand")
            raise _Finished()
        return resolution

    async def _session_hint(self) -> str | None:
        if not self.profile.session_cookie:
            return None
        value = await self.screen.cookie(self.profile.session_cookie)
        return value[-4:] if value else None

    async def _escalate(self, code: str, reason: str, step: Step | None) -> str:
        """A known screen that needs a person (e.g. a one-time code)."""
        if self.handoff is None:
            raise StepFailed("ESCALATION_UNRESOLVED", "a human operator", f"{code}: {reason} "
                             "(no operator connected; start the control room and use --operator)")
        rule = self.profile.screens.get(code) or self._cap.outcomes.get(code)
        hints = {"session": await self._session_hint() or "?"}
        steps = ticket_instructions.known_screen(code, rule, reason, hints) if rule else \
            ticket_instructions.stuck(step, self._params, f"It stopped because {reason}",
                                      await self._screen_title())
        await self._ticket("needs_human", f"{code.replace('_', ' ').capitalize()}", reason, steps, step)
        self._resume_index = await self._find_resume_point(step)
        return "resume_at"

    async def _stuck(self, step: Step, e: "StepFailed") -> int:
        """Replay cannot find its way: ask a person, then continue from what is on screen.
        The ticket says it in plain words; the technical detail goes to the run log."""
        self.log.event("stuck", f"{e.kind}: expected {e.expected}; observed {e.observed}",
                       step_id=step.id, kind=e.kind)
        problem = self.log.redactor.text(e.plain) if e.plain else ticket_instructions.problem(e.kind, step)
        steps = ticket_instructions.stuck(step, self._params, problem, await self._screen_title())
        await self._ticket("stuck", f"Stuck: {substitute_text(step.intent, self._params).rstrip('.')}",
                           f"{problem}.", steps, step)
        return await self._find_resume_point(step)

    async def _staff_request(self, step: Step, request: str, by: str) -> int:
        """Staff asked to stop or step in. Both happen here, between steps, so no step is ever
        left half done. Returns the step index to continue from."""
        if request == "stop":
            raise StepFailed("STOPPED_BY_OPERATOR", f"the run to continue with step {step.id}",
                             f"stopped by {by}" + (" after an irreversible step" if self._committed else
                                                    "; nothing irreversible had run"))
        await self._ticket("takeover", f"Taken over by {by}", f"{by} asked to take over before step {step.id}",
                           ticket_instructions.takeover(step, self._params, by), step)
        return await self._find_resume_point(step)

    async def _human_commit(self, step: Step, res) -> "_Jump":
        """An irreversible step with no --allow-irreversible: a person reviews the paused screen
        (view only) and decides. On approval the automation performs exactly that step, on the
        element it already checked, and records who approved it."""
        if self.handoff is None:
            raise StepFailed("APPROVAL_REQUIRED", f"approval for step {step.id}",
                             "irreversible step; no operator connected and no --allow-irreversible")
        decision = self.screen.policy.authorize(step.action, res.element, res.frame_url)
        if not decision.allowed:
            raise StepFailed("POLICY_BLOCKED", f"step {step.id} within the allowlist", decision.reason)
        name = res.element.get("name") or step.id
        self.log.event("approval_requested", f"{step.id}: '{name}' is irreversible; asking a person")
        resolution = await self._ticket(
            "approval", f"Approve: {substitute_text(step.intent, self._params)}",
            f"'{name}' cannot be undone, so a person must decide",
            ticket_instructions.approval(self._cap, step, self._params, name), step)
        if resolution.record.resolution == "rejected":
            self._result.status = "business_outcome"
            self._result.outcome = BusinessOutcome(
                code="REJECTED_BY_OPERATOR", step_id=step.id,
                message=f"{resolution.record.operator or 'The operator'} rejected '{name}'; nothing was submitted")
            raise _Finished()
        if resolution.record.resolution != "approved":  # only an explicit approval commits
            raise StepFailed("ESCALATION_UNRESOLVED", f"approval for step {step.id}",
                             f"the ticket ended as {resolution.record.resolution}; nothing was submitted")
        self.log.event("approval_given", f"{step.id}: {resolution.record.operator or 'the operator'} "
                       f"approved '{name}'; the automation performs it", step_id=step.id)
        self._approved_step = step.id
        return _Jump(self._cap.steps.index(step))  # run this step again, now approved

    async def _find_resume_point(self, step: Step | None) -> int:
        start = self._cap.steps.index(step) if step else 0
        for i in range(start, len(self._cap.steps)):
            if (await self.screen.resolve(self._cap.steps[i].target, self._params)).found:
                self.log.event("resumed", f"resuming at step {self._cap.steps[i].id}")
                return i
        if (await self.known.holds(self.profile.signed_in, {}))[0]:
            self.log.event("resumed", "resuming from step 1")
            return 0
        raise StepFailed("UNKNOWN_STATE", "a screen where a step can resume", await self._observed())

    # ------------------------------------------------------------ evidence

    async def _fail(self, e: StepFailed) -> None:
        shot = None
        dom = None
        if self.screen.page is not None and e.kind not in ("INPUT_INVALID", "APPROVAL_REQUIRED"):
            await self._register_screen_pii()
            shot = await self.screen.screenshot(self.log.path("failure.png"),
                                                 self.log.redactor.sensitive_values())
            dom = self.log.write_text(self.screen.snapshot_name, await self.screen.dom_snapshot())
        self._result.status = "failed"
        self._result.error = RunError(kind=e.kind, step_id=self._current_step_id, expected=e.expected,
                                      observed=self.log.redactor.text(e.observed),
                                      retryable=e.retryable and not self._committed,
                                      # Relative to the run folder, so results move with it.
                                      screenshot=shot.name if shot else None,
                                      dom_snapshot=dom.name if dom else None)

    async def _screen_title(self) -> str:
        """The title of the screen the run is on, for a person reading a ticket."""
        try:
            screens = await self.screen.frame_texts()
        except ScreenError:
            return "a page that could not be read"
        main = screens.get("main") or screens.get("top") or next(iter(screens.values()), None)
        return f"'{main['title']}'" if main and main["title"] else "a page with no title"

    async def _observed(self) -> str:
        """A short description of what is on screen, for error messages."""
        try:
            screens = await self.screen.frame_texts()
        except ScreenError:
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


@dataclass
class _Jump:
    """Continue the run at this step index (after a person performed a step)."""
    index: int


def substitute_text(text: str, params: dict[str, str]) -> str:
    return re.sub(r"\$\{([a-z_][a-z0-9_]*)\}", lambda m: params.get(m.group(1), m.group(0)), text)


def _condition(d: dict) -> Condition:
    return Condition(**{k: v for k, v in d.items() if v is not None})


def _target_text(step: Step) -> str:
    s = step.target.strategies[0]
    return f"{s.kind} {s.model_dump(exclude={'kind'})}"


def _expect_text(step: Step) -> str:
    return ", ".join(f"{k} {v!r}" for k, v in step.expect.model_dump(exclude_none=True).items())
