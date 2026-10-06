"""The web backend: requests from the Ask page run as background jobs; staff work the shared
ticket inbox and review capabilities in the control room.

Each request becomes a job. The router either reuses an approved capability (replay, no AI),
asks for a missing input, or runs discovery and saves a draft for approval. Browsers run
headless; each job keeps a live JPEG of its screen for the page, never written to disk.
"""

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .. import capability_workflows
from ..catalog import capability_review, request_router
from ..catalog.capability_store import CapabilityStore
from ..config_files import load_app_profile
from ..discovery.llm_clients import (
    LLMClient,
    ScriptedClient,
    configured_client,
    llm_configured,
)
from ..human_handoff.control_lease import SessionController
from ..human_handoff.tickets import LocalTicketInbox, TicketInbox
from ..models.capability import Capability
from ..screen.screen_interface import Screen
from ..settings import (
    DEFAULT_APP,
    LIVE_VIEW_INTERVAL_S,
    SCRIPTED_DISCOVERY_DIR,
    VALIDATION_RUNS,
)


def demo_script_for(query: str) -> Path | None:
    """An offline demo script whose `match` words all appear in the request, if any."""
    words = set(query.lower().replace("-", " ").split())
    for path in sorted(SCRIPTED_DISCOVERY_DIR.glob("*.yaml")):
        match = [w.lower() for w in (yaml.safe_load(path.read_text(encoding="utf-8")).get("match") or [])]
        if match and all(any(w in word for word in words) for w in match):
            return path
    return None

def catalog() -> list[Capability]:
    """The newest approved version of each capability (what the router may choose from),
    plus the newest version of each when that one is not approved (a draft waiting for
    review, or a rejected one kept for the record)."""
    newest: dict[str, Capability] = {}
    approved: dict[str, Capability] = {}
    for cap in CapabilityStore().all():  # sorted by name, then version
        newest[cap.name] = cap
        if cap.status == "approved":
            approved[cap.name] = cap
    return [*approved.values(), *(c for c in newest.values() if c.status != "approved")]

# How a request ended when a person closed its ticket, in words for the person who asked.
STAFF_ENDINGS = {
    "completed_by_human": "A staff member took care of this by hand.",
    "aborted": "A staff member cancelled this request.",
    "rejected": "A staff member decided not to go ahead. Nothing was changed.",
    "timed_out": "It needed a staff member, and nobody was available in time.",
}
# The request's status when a person ended it on its ticket. A ticket nobody took stays a failure.
STAFF_JOB_STATUS = {"completed_by_human": "done_by_staff", "aborted": "cancelled", "rejected": "cancelled"}


def ended_by_staff(result) -> str | None:
    """`done_by_staff` or `cancelled` if a person ended this run on its ticket, else None."""
    if result.status in ("success", "business_outcome") or not result.handoffs:
        return None
    return STAFF_JOB_STATUS.get(result.handoffs[-1].resolution)


def plain_discovery_message(outcome: capability_workflows.DiscoverOutcome) -> str:
    """How a discovery ended, in words for the person who asked (the details stay for staff)."""
    result = outcome.result
    if outcome.saved_to:
        return ("Done. This was a new kind of request, so a staff member will check how it was "
                "done before it's used again.")
    ended_by_person = STAFF_ENDINGS.get(result.handoffs[-1].resolution) if result.handoffs else None
    if ended_by_person:
        return ended_by_person
    if result.reason.startswith("not allowed"):
        blocked = re.search(r'"([^"]+)"', result.reason)
        what = f" ('{blocked.group(1)}')" if blocked else ""
        return (f"This needs something the system isn't allowed to do on its own{what}. "
                "A staff member has to do it.")
    if "LLM unavailable" in result.reason:
        return ("Learning new kinds of requests isn't available right now. Requests the system "
                "already knows still work.")
    if result.status == "done":
        return ("The system worked out the steps once but couldn't repeat them reliably, so "
                "nothing was saved. A staff member can look at what happened.")
    return ("The system couldn't work out how to do this by itself. A staff member can see "
            "what happened in the control room.")


@dataclass
class Job:
    id: str
    query: str
    slow_mo: int
    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = "routing"
    route: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    result: dict | None = None
    needs: dict | None = None
    output_labels: dict[str, str] = field(default_factory=dict)  # output name -> description
    step_text: dict[str, str] = field(default_factory=dict)  # step id -> what it did, in plain words
    ticket_id: str | None = None
    screen: Screen | None = None
    controller: SessionController | None = None
    frame: bytes | None = None
    live: bool = False

    def on_event(self, record: dict) -> None:
        if record.get("message") and record["type"] != "policy_decision":
            step = {"t": record["ts"][11:19], "type": record["type"], "message": record["message"]}
            if record["type"] == "step_done" and record.get("step_id") in self.step_text:
                step["plain"] = self.step_text[record["step_id"]]
            self.steps.append(step)

    def note(self, message: str, kind: str = "note") -> None:
        self.steps.append({"t": datetime.now(UTC).strftime("%H:%M:%S"), "type": kind,
                           "message": message})

    async def attach(self, screen: Screen, controller: SessionController | None) -> None:
        self.screen, self.controller, self.live = screen, controller, True
        asyncio.create_task(self._frames(screen))

    async def _frames(self, screen: Screen) -> None:
        while self.live and self.screen is screen:
            self.frame = await screen.capture_frame() or self.frame
            await asyncio.sleep(LIVE_VIEW_INTERVAL_S)

    def summary(self) -> dict:
        return {"id": self.id, "query": self.query, "status": self.status, "route": self.route,
                "ticket_id": self.ticket_id, "created": self.created.isoformat()}

    def detail(self) -> dict:
        held = bool(self.controller and self.controller.human_in_control)
        return {**self.summary(), "steps": self.steps[-200:], "result": self.result,
                "needs": self.needs, "output_labels": self.output_labels, "live": self.live,
                "has_frame": self.frame is not None,
                "controllable": self.controller is not None,
                "held_by_human": held,
                "owner": self.controller.owner if self.controller else "automation"}

class ControlRoom:
    """Jobs, tickets and settings shared by the Ask page and the control room."""

    def __init__(self, bank_url: str, slow_mo: int = 0):
        self.slow_mo = slow_mo  # 0 = fast (real timings); set from the control room's demo tools
        # Demo setting: let the requests page watch the bank screen, read-only. A real requester
        # would not see it (it shows other members' data); staff always can, in the control room.
        self.show_screen_to_requester = True
        self.inbox = TicketInbox()
        self.jobs: dict[str, Job] = {}
        self.ticket_jobs: dict[str, str] = {}  # ticket id -> job id that raised it
        self.bank_url = bank_url
        self.profile = load_app_profile(DEFAULT_APP)
        self.ai = {"configured": llm_configured(), "last_error": None}

    def llm(self) -> LLMClient | None:
        return configured_client()

    async def ask(self, query: str, capability: str | None = None,
                  params: dict[str, str] | None = None, learn: bool = False,
                  how: str = "inputs given in the form") -> Job:
        job = Job(id=f"J-{secrets.token_hex(2)}", query=query, slow_mo=self.slow_mo)
        self.jobs[job.id] = job
        asyncio.create_task(self._run(job, capability, params or {}, learn, how))
        return job

    async def _run(self, job: Job, forced: str | None, params: dict, learn: bool = False,
                   how: str = "inputs given in the form") -> None:
        try:
            caps = catalog()
            if learn:
                route = request_router.Route("discover", note="you asked to learn a new capability")
            elif forced:
                cap = next((c for c in caps if c.name == forced), None) or CapabilityStore().load(forced)
                route = request_router.Route("replay", cap, {k: str(v) for k, v in params.items()},
                                     note=how)
            else:
                job.note("Checking whether this is something we already know how to do")
                route = await request_router.route(job.query, caps, self.llm())
                if "AI unavailable" in route.note:
                    self.ai["last_error"] = route.note
                elif route.how == "llm":
                    self.ai["last_error"] = None  # the AI answered: it is available again
            job.route = {"kind": route.kind, "capability": route.capability.ref if route.capability else None,
                         "params": route.params, "how": route.how, "note": route.note}
            if route.kind == "clarify":
                job.status = "needs_input"
                job.needs = {"capability": route.capability.name if route.capability else None,
                             "inputs": {k: {"value": route.params.get(k, ""), "type": v.type,
                                            "description": v.description}
                                        for k, v in (route.capability.inputs.items() if route.capability else [])},
                             "message": route.note}
                job.note(f"Needs one more detail: {route.note}")
                return
            if route.kind == "replay":
                await self._replay(job, route.capability, route.params)
            else:
                await self._discover(job)
        except Exception as e:  # noqa: BLE001 - a job must always end with a visible status
            job.status = "failed"
            job.result = {"status": "failed", "message": "Something went wrong on our side.",
                          "detail": f"{type(e).__name__}: {e}"}
            job.note(f"Unexpected error: {type(e).__name__}: {e}", "error")
        finally:
            job.live = False

    async def _replay(self, job: Job, cap: Capability, params: dict[str, str]) -> None:
        job.status = "running"
        job.note("We've done this before, so the saved steps are used (no AI needed)")
        if cap.status != "approved":
            job.status, job.result = "failed", {"status": "failed",
                                                "message": f"{cap.ref} is a draft; approve it first"}
            return
        job.output_labels = {k: v.description or k for k, v in cap.outputs.items()}
        job.step_text = {s.id: capability_review.done_sentence(s, params) for s in cap.steps}
        log = capability_workflows.new_run_log("replay", self.profile, quiet=True, listeners=[job.on_event])
        result = await capability_workflows.run_capability(cap, params, self.profile, log, inbox=LocalTicketInbox(self.inbox),
                                       slow_mo=job.slow_mo, on_ready=job.attach,
                                       on_ticket=self._ticket_hook(job))
        job.status = ended_by_staff(result) or result.status
        job.result = result.model_dump(mode="json")  # the full result contract

    def _ticket_hook(self, job: Job):
        def hook(ticket_id: str | None) -> None:
            job.ticket_id = ticket_id
            if ticket_id:
                self.ticket_jobs[ticket_id] = job.id
        return hook

    async def _discover(self, job: Job) -> None:
        ai_down = not llm_configured() or self.ai["last_error"]
        script = demo_script_for(job.query) if ai_down else None
        if ai_down and script is None:
            job.status = "failed"
            job.result = {"status": "failed", "message": (
                "This is a new kind of request, and learning new ones isn't available right now. "
                "Requests the system already knows still work.")}
            return
        await self._discover_with(job, script)
        if job.status == "failed" and self.ai["last_error"] and not script:
            script = demo_script_for(job.query)
            if script:
                job.note("The AI hit its usage limit, so prepared example steps are used instead")
                await self._discover_with(job, script)

    async def _discover_with(self, job: Job, script: Path | None) -> None:
        """Discover with the offline `script` if given, else with the configured model."""
        job.status = "discovering"
        if script:
            job.note(f"This is new. Learning is offline right now, so prepared example steps "
                     f"are used ({script.name}); everything else runs for real")
        else:
            job.note("This is new, so the AI is working out the steps on the bank system")

        checks = iter(range(1, VALIDATION_RUNS + 1))

        def validation_log():
            job.note(f"Testing the new steps: run {next(checks)} of {VALIDATION_RUNS}, without the AI")
            return capability_workflows.new_run_log("validate", self.profile, quiet=True, listeners=[job.on_event])

        log = capability_workflows.new_run_log("discover", self.profile, quiet=True, listeners=[job.on_event])
        llm = ScriptedClient(script) if script else configured_client(log)  # logs its prompts
        outcome = await capability_workflows.learn_capability(
            job.query, self.profile, log, llm, inbox=LocalTicketInbox(self.inbox), slow_mo=job.slow_mo,
            on_ready=job.attach, on_ticket=self._ticket_hook(job),
            validation_log=validation_log, validation_ready=job.attach)
        outputs = {k: v["value"] for k, v in outcome.result.outputs.items()}
        if "429" in outcome.message or "quota" in outcome.message.lower():
            self.ai["last_error"] = "the AI's usage limit is reached"
        job.status = "discovered" if outcome.saved_to else ended_by_staff(outcome.result) or "failed"
        job.result = {"status": job.status, "message": plain_discovery_message(outcome),
                      "detail": outcome.message, "outputs": outputs,
                      "capability": outcome.capability.ref if outcome.saved_to else None}
