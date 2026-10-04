"""Shared machinery for acting on a screen step by step: parameter and secret values,
screen conditions, detectors for known screens, waiting, and sign-on.

Discovery uses it to sign on before the LLM takes over (so credentials never reach a
model). Replay uses it for every step.
"""

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .evidence import RunLog
from .schema import AppProfile, Condition, OutcomeRule
from .surface.web import PolicyViolation, WebSurface, substitute

SECRET_REF = re.compile(r"^\$\{secret:([A-Z][A-Z0-9_]*)\}$")


class SignOnFailed(Exception):
    pass


@dataclass
class Detected:
    """A known screen state, matched by an outcome rule."""
    code: str
    rule: OutcomeRule
    message: str


class Executor:
    def __init__(self, surface: WebSurface, profile: AppProfile, log: RunLog,
                 outcomes: dict[str, OutcomeRule] | None = None):
        self.surface = surface
        self.profile = profile
        self.log = log
        # Capability-specific outcomes are checked before the app-wide screens.
        self.outcomes = outcomes or {}

    def value(self, raw: str, params: dict[str, str]) -> str:
        """Turn a step value into the text to type: ${secret:NAME}, ${param} or a literal."""
        m = SECRET_REF.match(raw)
        if not m:
            return substitute(raw, params)
        name = m.group(1)
        if name not in self.profile.secrets:
            raise PolicyViolation(f"secret {name} is not declared by app profile {self.profile.id}")
        secret = os.environ.get(name)
        if not secret:
            raise SignOnFailed(f"secret {name} is not set in the environment")
        self.log.redactor.register(secret, "secret")
        return secret

    async def holds(self, cond: Condition, params: dict[str, str],
                    screens: dict | None = None) -> tuple[bool, str]:
        """Does the condition hold on the current screen? Returns (holds, captured message)."""
        screens = screens if screens is not None else await self.surface.screens()
        c = substitute(cond.model_dump(), params)
        if c["frame_present"] and c["frame_present"] not in screens:
            return False, ""
        if not (c["text"] or c["regex"] or c["title_contains"]):
            return True, ""
        if c["frame"]:
            pool = [screens[c["frame"]]] if c["frame"] in screens else []
        else:
            pool = list(screens.values())
        for screen in pool:
            if c["text"] and c["text"] not in screen["text"]:
                continue
            if c["title_contains"] and c["title_contains"] not in screen["title"]:
                continue
            message = c["text"] or c["title_contains"] or ""
            if c["regex"]:
                m = re.search(c["regex"], screen["text"])
                if not m:
                    continue
                message = (m.group(1) if m.groups() else m.group(0)).strip()
            return True, message
        return False, ""

    async def detect(self, params: dict[str, str]) -> Detected | None:
        screens = await self.surface.screens()
        for code, rule in [*self.outcomes.items(), *self.profile.screens.items()]:
            ok, message = await self.holds(rule.when, params, screens)
            if ok:
                return Detected(code, rule, rule.message or message or code)
        return None

    async def wait_until(self, ready: Callable[[], Awaitable[bool]], params: dict[str, str],
                         timeout: float, detect: bool = True) -> str | Detected:
        """Poll until `ready()` is true ("ready"), a known screen appears (a Detected),
        or the timeout passes ("timeout"). Known screens win over readiness, because an
        error page can still contain the control the next step wants."""
        deadline = time.monotonic() + timeout
        while True:
            if detect and (found := await self.detect(params)):
                return found
            if await ready():
                return "ready"
            if time.monotonic() >= deadline:
                return "timeout"
            await asyncio.sleep(0.25)

    async def sign_on(self) -> Detected | None:
        """Run the app profile's sign-on steps. Returns a Detected if a known screen
        (for example an identity check) interrupts sign-on."""
        await self.surface.goto(self.profile.base_url + self.profile.entry_path)
        for step in self.profile.login:
            res = None

            async def found(target=step.target) -> bool:
                nonlocal res
                res = await self.surface.resolve(target, {})
                return res.found

            outcome = await self.wait_until(found, {}, step.timeout_s)
            if isinstance(outcome, Detected):
                return outcome
            if outcome == "timeout":
                raise SignOnFailed(f"sign-on step {step.id}: control not found")
            value = self.value(step.value, {}) if step.value is not None else None
            await self.surface.act(res, step.action, value)
            self.log.event("sign_on", f"{step.id}: {step.intent}", step_id=step.id)

        async def signed_in() -> bool:
            return (await self.holds(self.profile.signed_in, {}))[0]

        outcome = await self.wait_until(signed_in, {}, 10)
        if isinstance(outcome, Detected):
            return outcome
        if outcome == "timeout":
            raise SignOnFailed("sign-on did not reach the signed-in screen (check the credentials)")
        return None

    async def app_version(self) -> str | None:
        probe = self.profile.version_probe
        if probe is None:
            return None
        screens = await self.surface.screens()
        texts = [screens[probe.frame]["text"]] if probe.frame in screens else \
            [s["text"] for s in screens.values()]
        for text in texts:
            if m := re.search(probe.regex, text):
                return m.group(1)
        return None
