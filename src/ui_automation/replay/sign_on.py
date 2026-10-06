"""Signing on to the app, and turning step values into the text to type.

Sign-on is an authored flow in the app profile, run by code with ${secret:NAME} references:
credentials are read from the environment at run time and never reach an LLM, an artifact or
a log (each is registered with the masker the moment it is read). The log records only that
sign-on succeeded.
"""

import os
import re

from ..models.app_profile import AppProfile
from ..safety.log_masking import Redactor
from ..screen.screen_interface import PolicyViolation, substitute
from ..settings import SIGNED_IN_TIMEOUT_S
from .known_screens import Detected, KnownScreens

SECRET_REF = re.compile(r"^\$\{secret:([A-Z][A-Z0-9_]*)\}$")


class SignOnFailed(Exception):
    """Sign-on could not reach the signed-in screen."""


def resolve_step_value(raw: str, params: dict[str, str], profile: AppProfile,
                       redactor: Redactor) -> str:
    """Turn a step value into the text to type: ${secret:NAME}, ${param} or a literal."""
    m = SECRET_REF.match(raw)
    if not m:
        return substitute(raw, params)
    name = m.group(1)
    if name not in profile.secrets:
        raise PolicyViolation(f"secret {name} is not declared by app profile {profile.id}")
    secret = os.environ.get(name)
    if not secret:
        raise SignOnFailed(f"secret {name} is not set in the environment")
    redactor.register(secret, "secret")
    return secret


async def sign_on(known: KnownScreens) -> Detected | None:
    """Run the app profile's sign-on steps. Returns a Detected if a known screen (for example
    an identity check) interrupts sign-on."""
    screen, profile, log = known.screen, known.profile, known.log
    await screen.goto(profile.base_url + profile.entry_path)
    for step in profile.login:
        res = None

        async def found(target=step.target) -> bool:
            nonlocal res
            res = await screen.resolve(target, {})
            return res.found

        outcome = await known.wait_until(found, {}, step.timeout_s)
        if isinstance(outcome, Detected):
            return outcome
        if outcome == "timeout":
            raise SignOnFailed(f"sign-on step {step.id}: control not found")
        value = None if step.value is None else resolve_step_value(step.value, {}, profile, log.redactor)
        await screen.act(res, step.action, value)

    async def signed_in() -> bool:
        return (await known.holds(profile.signed_in, {}))[0]

    outcome = await known.wait_until(signed_in, {}, SIGNED_IN_TIMEOUT_S)
    if isinstance(outcome, Detected):
        return outcome
    if outcome == "timeout":
        raise SignOnFailed("sign-on did not reach the signed-in screen (check the credentials)")
    return None


async def read_app_version(known: KnownScreens) -> str | None:
    """The app's version string (e.g. from its banner), used to spot version drift."""
    probe = known.profile.version_probe
    if probe is None:
        return None
    texts = await known.screen.frame_texts()
    candidates = [texts[probe.frame]["text"]] if probe.frame in texts else \
        [t["text"] for t in texts.values()]
    for text in candidates:
        if m := re.search(probe.regex, text):
            return m.group(1)
    return None
