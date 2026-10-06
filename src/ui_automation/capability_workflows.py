"""The two things the system does, as functions both the CLI and the web pages call:

  learn_capability(goal)   the AI accomplishes the goal live -> compile -> validation replay in a
                           fresh browser -> saved as a draft capability
  run_capability(cap)      deterministic replay of a capability, no AI; on the web surface
                           (DOM) by default, or on the pixel surface (screenshots and mouse only)
"""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .catalog.capability_store import CapabilityStore
from .discovery.discovery_agent import DiscoveryAgent, DiscoveryResult
from .discovery.llm_clients import LLMClient
from .discovery.llm_privacy import mask_request
from .discovery.recipe_builder import CompileError, compile_capability, slugify
from .evidence_writer import RunLog
from .human_handoff.control_lease import SessionController
from .human_handoff.tickets import TicketInboxClient
from .models.app_profile import AppProfile
from .models.capability import Capability
from .models.run_result import RunResult
from .replay.known_screens import KnownScreens
from .replay.replay_runner import ReplayRunner, parse_output
from .safety.log_masking import Redactor
from .safety.safety_policy import SafetyPolicy
from .screen.browser_screen import BrowserScreen
from .screen.screen_interface import Screen
from .settings import DISCOVERY_MAX_STEPS, TICKET_TIMEOUT_S, VALIDATION_RUNS

if TYPE_CHECKING:
    from .screen.pixel_screen import PixelSurface

OnReady = Callable[[Screen, SessionController | None], Awaitable[None]]
OnTicket = Callable[[str | None], Awaitable[None] | None]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def new_run_log(kind: str, profile: AppProfile, quiet: bool = False, listeners: list | None = None) -> RunLog:
    return RunLog(kind, Redactor(list(profile.pii_patterns.values())), quiet=quiet, listeners=listeners)


@asynccontextmanager
async def browser_session(log: RunLog, *, headed: bool = False, inbox: TicketInboxClient | None = None,
                          slow_mo: int = 0, on_ready: OnReady | None = None,
                          on_ticket: OnTicket | None = None, keep_open: bool = False,
                          policy: SafetyPolicy | None = None, ticket_timeout_s: float = TICKET_TIMEOUT_S,
                          pixel: "PixelSurface | None" = None, profile: AppProfile | None = None):
    """A screen for one run: the browser's DOM, or with `pixel`, a window read only through
    screenshots. With an inbox, also a session controller (so the run can raise tickets) and a
    localhost debugging port that operator tools can attach to."""
    policy = policy or SafetyPolicy.load()
    if pixel is not None:
        from .screen.pixel_screen import PixelScreen  # needs the optional OCR packages

        screen = PixelScreen(policy, log, profile.pixel_regions if profile else {}, pixel)
    else:
        screen = BrowserScreen(policy, log)
    controller = None
    try:
        await screen.start(headed=headed, debug_port=free_port() if inbox else None, slow_mo=slow_mo)
        if inbox is not None:
            controller = SessionController(screen, log, inbox, timeout_s=ticket_timeout_s,
                                           on_ticket=on_ticket)
            await controller.attach()
        if on_ready is not None:
            await on_ready(screen, controller)
        yield screen, controller
    finally:
        if keep_open and headed:
            await wait_for_enter()
        await screen.close()


async def run_capability(cap: Capability, params: dict[str, str], profile: AppProfile, log: RunLog, *,
                 headed: bool = False, inbox: TicketInboxClient | None = None,
                 allow_irreversible: bool = False, stop_before_irreversible: bool = False,
                 slow_mo: int = 0, on_ready: OnReady | None = None,
                 on_ticket: OnTicket | None = None, keep_open: bool = False,
                 policy: SafetyPolicy | None = None, ticket_timeout_s: float = TICKET_TIMEOUT_S,
                 pixel: "PixelSurface | None" = None) -> RunResult:
    """Replay `cap`. With `pixel`, on the pixel surface: no DOM is read at all."""
    try:
        async with browser_session(log, headed=headed, inbox=inbox, slow_mo=slow_mo,
                                   on_ready=on_ready, on_ticket=on_ticket, keep_open=keep_open,
                                   policy=policy, ticket_timeout_s=ticket_timeout_s,
                                   pixel=pixel, profile=profile) as (screen, controller):
            engine = ReplayRunner(screen, profile, log, handoff=controller)
            return await engine.run(cap, params, allow_irreversible=allow_irreversible,
                                    stop_before_irreversible=stop_before_irreversible)
    finally:
        log.close()


@dataclass
class DiscoverOutcome:
    result: DiscoveryResult
    capability: Capability | None = None
    saved_to: Path | None = None
    validation: RunResult | None = None  # the last validation replay that ran
    validations: list[RunResult] = field(default_factory=list)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.capability is not None and self.validation is not None and \
            self.validation.status == "success"


def recorded_steps(result: DiscoveryResult) -> dict:
    """Every step the AI took during discovery, with the locators proven for each."""
    return {
        "status": result.status, "reason": result.reason, "summary": result.summary,
        "spec": result.spec.model_dump() if result.spec else None,
        "app_version": result.app_version, "model": result.model,
        "steps": [{**asdict(s), "strategies": [x.model_dump() for x in s.strategies]}
                  for s in result.trace],
        "outputs": result.outputs,
    }


async def learn_capability(goal: str, profile: AppProfile, log: RunLog, llm: LLMClient, *,
                   headed: bool = False, inbox: TicketInboxClient | None = None,
                   max_steps: int = DISCOVERY_MAX_STEPS,
                   allow_irreversible: bool = False, save: bool = True, slow_mo: int = 0,
                   on_ready: OnReady | None = None, on_ticket: OnTicket | None = None,
                   validation_log: Callable[[], RunLog] | None = None,
                   validation_ready: OnReady | None = None,
                   policy: SafetyPolicy | None = None,
                   store: CapabilityStore | None = None) -> DiscoverOutcome:
    """LLM run, then compile, then VALIDATION_RUNS replays in fresh browsers, then save as draft."""
    model = llm.models[0] if hasattr(llm, "models") else "scripted"
    for value in mask_request(goal).values.values():  # masked in every log line from here on
        log.redactor.register(value, "pii")
    log.event("run_started", f"goal: {goal} (model: {model})", goal=goal, app=profile.id,
              model=model, allow_irreversible=allow_irreversible)
    try:
        async with browser_session(log, headed=headed, inbox=inbox, slow_mo=slow_mo, policy=policy,
                                   on_ready=on_ready, on_ticket=on_ticket) as (screen, controller):
            agent = DiscoveryAgent(screen, KnownScreens(screen, profile, log), llm, log,
                                   max_steps=max_steps, handoff=controller,
                                   allow_irreversible=allow_irreversible)
            try:
                result = await agent.run(goal)
            except Exception as e:  # noqa: BLE001 - top-level boundary: keep the evidence whatever broke
                log.event("error", f"{type(e).__name__}: {e}")
                result = agent.result
                result.status, result.reason = "failed", f"{type(e).__name__}: {e}"
            if result.status != "done":
                await screen.screenshot(log.path("failure.png"), log.redactor.sensitive_values())
            await screen.stop_trace(log.path("trace.zip") if result.status != "done" else None)
            log.write_json("recorded-steps.json", recorded_steps(result))
        outcome = DiscoverOutcome(result, message=f"discovery {result.status}: {result.reason}")
        if result.status != "done":
            return outcome
        store = store or CapabilityStore()
        try:
            cap = compile_capability(result, profile, log.run_id, store.next_version(
                profile.id, slugify(result.spec.capability_name)))
        except CompileError as e:
            outcome.message = f"could not compile a capability: {e}"
            return outcome
        log.write_text("candidate.yaml", yaml.safe_dump(cap.model_dump(mode="json", exclude_none=True),
                                                        sort_keys=False))
        log.event("compiled", f"{cap.ref}: {len(cap.steps)} steps, risk {cap.risk}", capability=cap.ref)
        outcome.capability = cap
    finally:
        log.close()  # also writes the deferred screens and prompts, fully redacted

    # Validation replays stop before the first irreversible step, so they can only confirm
    # the outputs read before that commit point. Every one must pass: a flaky recipe is not saved.
    params = {i.name: i.value for i in result.spec.inputs}
    commit = next((i for i, s in enumerate(cap.steps) if s.risk == "irreversible"), len(cap.steps))
    before_commit = {s.output for s in cap.steps[:commit] if s.action == "extract"}
    expected = {k: parse_output(v["value"], cap.outputs[k]) for k, v in result.outputs.items()
                if k in before_commit}
    for n in range(1, VALIDATION_RUNS + 1):
        vlog = validation_log() if validation_log else new_run_log("validate", profile)
        check = await run_capability(cap, params, profile, vlog, headed=headed,
                                     stop_before_irreversible=True, on_ready=validation_ready,
                                     policy=policy)
        outcome.validation = check
        outcome.validations.append(check)
        if check.status != "success" or any(check.outputs.get(k) != v for k, v in expected.items()):
            outcome.message = (f"validation replay {n} of {VALIDATION_RUNS}: {check.status}; "
                               "the candidate was not saved")
            return outcome
    cap.provenance.validated_by_run = check.run_id
    cap.provenance.validation_runs = [c.run_id for c in outcome.validations]
    if save:
        outcome.saved_to = store.save(cap)
        outcome.message = f"saved {cap.ref} as a draft"
    else:
        outcome.message = f"validated {cap.ref}; not saved"
    return outcome


async def wait_for_enter(prompt: str = "Press Enter to close the browser... ") -> None:
    await asyncio.to_thread(input, prompt)
