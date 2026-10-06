"""Who controls the live session, and how a ticket hands it to a person and back.

Control model
-------------
One browser session, one owner at a time. The lease is `owner` plus an `epoch` number:

  automation --(ticket raised)--> nobody (paused) --(ticket taken)--> human:<operator>
       ^                                                                   |
       +-----------------(ticket resolved: epoch += 1)---------------------+

An approval ticket is a decision, not a handover: taking it leaves the session paused and the
screen view only, so the screen the person approves is exactly the one the automation acts on.

The screen adapter calls `check_lease` before every action, so automation cannot act while
a person holds the session. Staff can also ask to step in ("take over") or stop a run; the run
honours the request at its next step boundary, never in the middle of a step. The person acts on the *same* live session: through the live
view in the control room (clicks and keys are forwarded here, and only accepted while they
hold the lease), or in the headed browser window for runs started from the CLI. A listener
in every frame records what they do; typed values are kept only as their length.
"""

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import typer

from ..evidence_writer import RunLog
from ..models.run_result import HandoffRecord
from ..screen.screen_interface import NotInControl, Screen
from ..settings import OPERATOR_PORT, TICKET_POLL_INTERVAL_S, TICKET_TIMEOUT_S
from .tickets import FINAL, NewTicket, TicketInboxClient

TicketKind = Literal["needs_human", "approval", "stuck", "takeover"]
StaffRequest = Literal["takeover", "stop"]

@dataclass
class HandoffResolution:
    record: HandoffRecord
    human_actions: list[dict] = field(default_factory=list)

class Handoff(Protocol):
    async def request(self, *, run_id: str, capability: str, step_id: str, kind: TicketKind,
                      title: str, reason: str, instructions: list[str], context: dict[str, str],
                      screenshot: Path | None) -> HandoffResolution:
        """Raise a ticket, pause automation, and return when the person resolves it
        (resumed, finished by hand, rejected, aborted) or nobody answers in time."""
        ...

    def pop_request(self) -> tuple[StaffRequest, str] | None:
        """A pending staff request (takeover or stop) and who made it, cleared on reading.
        Runs call this between steps."""
        ...

OPERATOR_RECORDER_JS = (Path(__file__).parent / "operator_recorder.js").read_text(encoding="utf-8")

RESOLUTION = {"resumed": "resumed", "done_by_human": "completed_by_human", "approved": "approved",
              "rejected": "rejected",
              "aborted": "aborted", "timed_out": "timed_out"}

@dataclass
class _Open:
    ticket_id: str
    actions: list[dict] = field(default_factory=list)
    unsent: list[dict] = field(default_factory=list)

class SessionController:
    def __init__(self, screen: Screen, log: RunLog, inbox: TicketInboxClient,
                 timeout_s: float = TICKET_TIMEOUT_S,
                 on_ticket: Callable[[str | None], Awaitable[None] | None] | None = None):
        self.screen = screen
        self.log = log
        self.inbox = inbox
        self.timeout_s = timeout_s
        self.on_ticket = on_ticket  # tells the control room which ticket this run is waiting on
        self.owner = "automation"
        self.epoch = 1
        self.open: _Open | None = None
        self._request: tuple[StaffRequest, str] | None = None

    async def attach(self) -> None:
        """Call after the browser starts and before the first navigation."""
        self.screen.lease_check = self.check_lease
        await self.screen.install_operator_recorder(self._on_human_event, OPERATOR_RECORDER_JS)

    def check_lease(self) -> None:
        if self.owner != "automation":
            raise NotInControl(f"the session is held by {self.owner or 'nobody (paused)'}")

    @property
    def human_in_control(self) -> bool:
        return self.owner.startswith("human:")

    # ------------------------------------------------------------ staff stepping in

    def ask_for(self, request: StaffRequest, by: str) -> None:
        """Staff asks to take over or stop. Honoured at the run's next step boundary."""
        if self.open is not None:
            raise ValueError("the run is already waiting on a ticket; use that ticket instead")
        self._request = (request, by)
        self.log.event("staff_request", f"{by} asked to {'take over' if request == 'takeover' else 'stop'}"
                       " the run; it will pause after the current step", request=request, by=by)

    def pop_request(self) -> tuple[StaffRequest, str] | None:
        request, self._request = self._request, None
        return request

    # ------------------------------------------------------------ the Handoff interface

    async def request(self, *, run_id: str, capability: str, step_id: str, kind: str, title: str,
                      reason: str, instructions: list[str], context: dict[str, str],
                      screenshot: Path | None) -> HandoffResolution:
        shot = base64.b64encode(screenshot.read_bytes()).decode() if screenshot else None
        ticket_id = await self.inbox.create(NewTicket(
            run_id=run_id, capability=capability, step_id=step_id, kind=kind, title=title,
            reason=reason, instructions=instructions, context=context, screenshot_png_b64=shot))
        self.open = _Open(ticket_id)
        if screenshot is not None and screenshot.exists():  # name it after its ticket
            screenshot = screenshot.replace(screenshot.with_name(f"ticket-{ticket_id}.png"))
        self.owner = ""  # paused: nobody may act until someone takes the ticket
        self.log.event("intervention_raised", f"ticket {ticket_id} ({kind}) at {step_id}: {reason}",
                       intervention=ticket_id, kind=kind, step_id=step_id, reason=reason)
        await self._notify(ticket_id)
        if not self.log.quiet:
            typer.secho(f"\n  >> Ticket {ticket_id}: {title}. Handle it in the control room: "
                        f"http://127.0.0.1:{OPERATOR_PORT}\n", fg=typer.colors.MAGENTA, bold=True)
        deadline = time.monotonic() + self.timeout_s
        state, operator, taken = "open", None, False
        while True:
            ticket = await self.inbox.get(ticket_id)
            state, operator = ticket["state"], ticket.get("operator")
            if state == "in_progress" and not taken:
                taken = True
                if kind == "approval":
                    self.log.event("intervention_taken", f"{operator} took ticket {ticket_id} to "
                                   "review; the screen stays view only", intervention=ticket_id,
                                   operator=operator)
                else:
                    self.owner = f"human:{operator}"
                    self.log.event("intervention_taken", f"{operator} took ticket {ticket_id} and "
                                   "holds the live session", intervention=ticket_id, operator=operator)
                    await self.screen.bring_to_front()
            await self._flush()
            if state in FINAL:
                break
            if time.monotonic() > deadline:
                await self.inbox.timed_out(ticket_id)
                state = "timed_out"
                break
            await asyncio.sleep(TICKET_POLL_INTERVAL_S)
        await self._flush()
        self.owner = "automation"
        self.epoch += 1
        actions = list(self.open.actions)
        self.log.event(f"intervention_{RESOLUTION[state]}", f"ticket {ticket_id}: {state.replace('_', ' ')}"
                       f" by {operator or 'nobody'}; automation holds the session again (epoch "
                       f"{self.epoch})", intervention=ticket_id, human_actions=len(actions))
        # The ticket as staff saw it, and how it ended (masked like every run file).
        self.log.write_json(f"ticket-{ticket_id}.json", {
            "ticket": ticket_id, "kind": kind, "title": title, "capability": capability,
            "step_id": step_id, "reason": reason,
            "context": {k: v for k, v in context.items() if not k.startswith("_")},
            "instructions": instructions, "state": state, "operator": operator,
            "human_actions": actions, "screenshot": screenshot.name if screenshot else None,
            "epoch_after": self.epoch})
        self.open = None
        await self._notify(None)
        record = HandoffRecord(intervention_id=ticket_id, reason=reason, operator=operator,
                               resolution=RESOLUTION[state], human_actions=len(actions))
        return HandoffResolution(record=record, human_actions=actions)

    async def _notify(self, ticket_id: str | None) -> None:
        if self.on_ticket is not None:
            result = self.on_ticket(ticket_id)
            if asyncio.iscoroutine(result):
                await result

    async def _flush(self) -> None:
        if self.open and self.open.unsent:
            batch, self.open.unsent = self.open.unsent, []
            await self.inbox.add_actions(self.open.ticket_id, batch)

    # ------------------------------------------------------------ the person's input

    def _require_human(self) -> None:
        if not self.human_in_control:
            raise NotInControl("take the ticket first: only the person holding it may act")

    async def human_click(self, x: float, y: float) -> None:
        self._require_human()
        await self.screen.click_at(x, y)

    async def human_type(self, text: str) -> None:
        self._require_human()
        await self.screen.type_text(text)

    async def human_press(self, key: str) -> None:
        self._require_human()
        await self.screen.press_key(key)

    def _on_human_event(self, _source, event: dict) -> None:
        if not self.human_in_control or self.open is None:
            return  # automation's own clicks also fire these listeners
        target = event.get("target") or {}
        summary = self.log.redactor.scrub({
            "action": event.get("type"), "role": target.get("role"), "name": target.get("name"),
            "label": target.get("label") or target.get("column"), "value": event.get("value")})
        self.open.actions.append(summary)
        self.open.unsent.append(summary)
        shown = f'{summary["action"]} {summary["role"]} "{summary["name"] or summary["label"] or ""}"'
        if summary["value"]:
            shown += f" <- {summary['value']}"
        self.log.event("human_action", shown, intervention=self.open.ticket_id, **summary)
