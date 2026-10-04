"""Human-in-the-loop handoff: the interface replay and discovery use to bring a person
onto the live session. The session controller (session.py) implements it."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .schema import Handoff as HandoffRecord


@dataclass
class HandoffResolution:
    record: HandoffRecord
    human_actions: list[dict] = field(default_factory=list)


class Handoff(Protocol):
    async def request(self, *, run_id: str, capability: str, step_id: str, reason: str,
                      screenshot: Path | None) -> HandoffResolution:
        """Pause automation, ask a person to act on the live session, and return when they
        hand control back (or abort, or nobody answers in time)."""
        ...
