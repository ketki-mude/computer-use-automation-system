"""Known screen states, and waiting on conditions.

A condition is a fact about the current screen: text visible, a frame title, a frame present.
App-wide known screens (session expired, maintenance notice, server error, identity check) come
from the app profile; a capability may add its own. Replay and sign-on check them while waiting
for the next step's target, so an error page is recognised instead of timing out.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..evidence_writer import RunLog
from ..models.app_profile import AppProfile
from ..models.capability import Condition, OutcomeRule
from ..screen.screen_interface import Screen, substitute
from ..settings import CONDITION_POLL_INTERVAL_S


@dataclass
class Detected:
    """A known screen state, matched by an outcome rule."""
    code: str
    rule: OutcomeRule
    message: str


class KnownScreens:
    """Checks conditions and known screens on one live screen."""

    def __init__(self, screen: Screen, profile: AppProfile, log: RunLog,
                 outcomes: dict[str, OutcomeRule] | None = None):
        self.screen = screen
        self.profile = profile
        self.log = log
        # Capability-specific outcomes are checked before the app-wide screens.
        self.outcomes = outcomes or {}
        screen.dialog_rules = profile.dialogs

    async def holds(self, cond: Condition, params: dict[str, str],
                    texts: dict | None = None) -> tuple[bool, str]:
        """Does the condition hold on the current screen? Returns (holds, captured message)."""
        texts = texts if texts is not None else await self.screen.frame_texts()
        c = substitute(cond.model_dump(), params)
        if c["frame_present"] and c["frame_present"] not in texts:
            return False, ""
        if not (c["text"] or c["regex"] or c["title_contains"]):
            return True, ""
        if c["frame"]:
            pool = [texts[c["frame"]]] if c["frame"] in texts else []
        else:
            pool = list(texts.values())
        for frame_text in pool:
            if c["text"] and c["text"] not in frame_text["text"]:
                continue
            if c["title_contains"] and c["title_contains"] not in frame_text["title"]:
                continue
            message = c["text"] or c["title_contains"] or ""
            if c["regex"]:
                m = re.search(c["regex"], frame_text["text"])
                if not m:
                    continue
                message = (m.group(1) if m.groups() else m.group(0)).strip()
            return True, message
        return False, ""

    async def detect(self, params: dict[str, str]) -> Detected | None:
        """The first known screen showing right now, capability outcomes first."""
        texts = await self.screen.frame_texts()
        for code, rule in [*self.outcomes.items(), *self.profile.screens.items()]:
            ok, message = await self.holds(rule.when, params, texts)
            if ok:
                return Detected(code, rule, rule.message or message or code)
        return None

    async def wait_until(self, ready: Callable[[], Awaitable[bool]], params: dict[str, str],
                         timeout: float, detect: bool = True) -> str | Detected:
        """Poll until `ready()` is true ("ready"), a known screen appears (a Detected), or the
        timeout passes ("timeout"). Known screens win over readiness, because an error page can
        still contain the control the next step wants."""
        deadline = time.monotonic() + timeout
        while True:
            if detect and (found := await self.detect(params)):
                return found
            if await ready():
                return "ready"
            if time.monotonic() >= deadline:
                return "timeout"
            await asyncio.sleep(CONDITION_POLL_INTERVAL_S)
