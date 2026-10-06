"""The ticket inbox: one queue for every run that needs a person.

Runs post a ticket when they get stuck or reach a risky step, then wait. An admin works
through the tickets one by one in the control room (web/):

  open --(take)--> in_progress --(resume)--> resumed        automation continues
                               --(done)----> done_by_human  the person finished the task
                               --(reject)--> rejected       approval refused; nothing submitted
  open | in_progress --(abort)-> aborted
  open | in_progress --(run gives up waiting)--> timed_out

Each ticket carries numbered instructions written for the admin. Tickets live in memory
only; the run that raised a ticket writes the redacted record into its own evidence.
"""

from datetime import UTC, datetime
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from ..settings import OPERATOR_PORT, TICKET_API_TIMEOUT_S

FIRST_TICKET_NUMBER = 101  # tickets read T-101, T-102, ...

Kind = Literal["needs_human", "approval", "stuck", "takeover"]
State = Literal["open", "in_progress", "resumed", "done_by_human", "rejected", "aborted", "timed_out"]
FINAL = {"resumed", "done_by_human", "rejected", "aborted", "timed_out"}

# Which buttons each kind of ticket offers while someone holds it: (action, label).
ACTIONS = {
    "needs_human": [("resume", "Hand back to automation"), ("done", "I finished it myself"),
                    ("abort", "Cancel the request")],
    "stuck": [("resume", "Hand back to automation"), ("done", "I finished it myself"),
              ("abort", "Cancel the request")],
    "approval": [("resume", "I clicked it, carry on"), ("reject", "Don't do it")],
    "takeover": [("resume", "Hand back to automation"), ("done", "I finished it myself"),
                 ("abort", "Cancel the request")],
}
MOVES = {  # action -> (target state, states it may come from)
    "take": ("in_progress", {"open"}),
    "resume": ("resumed", {"in_progress"}),
    "done": ("done_by_human", {"in_progress"}),
    "reject": ("rejected", {"in_progress"}),
    "abort": ("aborted", {"open", "in_progress"}),
    "timed_out": ("timed_out", {"open", "in_progress"}),
}


class NewTicket(BaseModel):
    run_id: str
    capability: str
    step_id: str
    kind: Kind
    title: str
    reason: str
    instructions: list[str]
    context: dict[str, str] = {}
    screenshot_png_b64: str | None = None


class Ticket(NewTicket):
    id: str
    created_at: datetime
    updated_at: datetime
    state: State = "open"
    operator: str | None = None
    human_actions: list[dict] = Field(default_factory=list)


class TicketInbox:
    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}
        self._count = FIRST_TICKET_NUMBER - 1

    def create(self, new: NewTicket) -> Ticket:
        self._count += 1
        now = datetime.now(UTC)
        t = Ticket(**new.model_dump(), id=f"T-{self._count}", created_at=now, updated_at=now)
        self.tickets[t.id] = t
        return t

    def move(self, ticket_id: str, to: State, *, allowed_from: set[str],
             operator: str | None = None) -> Ticket:
        t = self.tickets.get(ticket_id)
        if t is None:
            raise KeyError(f"no ticket {ticket_id}")
        if t.state not in allowed_from:
            raise ValueError(f"{ticket_id} is {t.state.replace('_', ' ')}; cannot {to.replace('_', ' ')}")
        t.state = to
        if operator:
            t.operator = operator
        t.updated_at = datetime.now(UTC)
        return t

    def act(self, ticket_id: str, action: str, operator: str | None = None) -> Ticket:
        t = self.tickets.get(ticket_id)
        if t is None:
            raise KeyError(f"no ticket {ticket_id}")
        if action not in ("take", "abort", "timed_out") and action not in dict(ACTIONS[t.kind]):
            raise ValueError(f"'{action}' is not available on a {t.kind.replace('_', ' ')} ticket")
        to, allowed = MOVES[action]
        return self.move(ticket_id, to, allowed_from=allowed, operator=operator)

    def view(self, t: Ticket) -> dict:
        d = t.model_dump(mode="json", exclude={"screenshot_png_b64"})
        d["has_screenshot"] = bool(t.screenshot_png_b64)
        d["buttons"] = ACTIONS[t.kind] if t.state == "in_progress" else []
        return d


# ---------------------------------------------------------------- how a run reaches the inbox

class TicketInboxClient(Protocol):
    async def create(self, ticket: NewTicket) -> str: ...
    async def get(self, ticket_id: str) -> dict: ...
    async def add_actions(self, ticket_id: str, actions: list[dict]) -> None: ...
    async def timed_out(self, ticket_id: str) -> None: ...

class LocalTicketInbox:
    """The inbox in this process (runs started from the control room)."""

    def __init__(self, inbox: TicketInbox):
        self.inbox = inbox

    async def create(self, ticket: NewTicket) -> str:
        return self.inbox.create(ticket).id

    async def get(self, ticket_id: str) -> dict:
        t = self.inbox.tickets[ticket_id]
        return {"state": t.state, "operator": t.operator}

    async def add_actions(self, ticket_id: str, actions: list[dict]) -> None:
        self.inbox.tickets[ticket_id].human_actions.extend(actions)

    async def timed_out(self, ticket_id: str) -> None:
        self.inbox.move(ticket_id, "timed_out", allowed_from={"open", "in_progress"})

class HttpTicketInbox:
    """The control room's inbox over HTTP (runs started from the CLI with --operator)."""

    def __init__(self, base_url: str = f"http://127.0.0.1:{OPERATOR_PORT}"):
        self.base_url = base_url

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=TICKET_API_TIMEOUT_S) as c:
                return (await c.get(f"{self.base_url}/api/tickets")).status_code == 200
        except httpx.HTTPError:
            return False

    async def create(self, ticket: NewTicket) -> str:
        async with httpx.AsyncClient(timeout=TICKET_API_TIMEOUT_S) as c:
            r = await c.post(f"{self.base_url}/api/tickets", json=ticket.model_dump())
            r.raise_for_status()
            return r.json()["id"]

    async def get(self, ticket_id: str) -> dict:
        async with httpx.AsyncClient(timeout=TICKET_API_TIMEOUT_S) as c:
            return (await c.get(f"{self.base_url}/api/tickets/{ticket_id}")).json()

    async def add_actions(self, ticket_id: str, actions: list[dict]) -> None:
        async with httpx.AsyncClient(timeout=TICKET_API_TIMEOUT_S) as c:
            await c.post(f"{self.base_url}/api/tickets/{ticket_id}/actions", json=actions)

    async def timed_out(self, ticket_id: str) -> None:
        async with httpx.AsyncClient(timeout=TICKET_API_TIMEOUT_S) as c:
            await c.post(f"{self.base_url}/api/tickets/{ticket_id}/timed_out")
