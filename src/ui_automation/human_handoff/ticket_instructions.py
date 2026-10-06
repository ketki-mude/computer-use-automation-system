"""Step-by-step instructions for the person who takes a ticket.

A ticket is only useful if someone who has never seen the run can act on it, so every
ticket says what happened, what to check, what to do, and which button ends it.
"""

import re

from ..models.capability import Capability, OutcomeRule, Step
from ..screen.screen_interface import substitute

TAKE = "Click 'Take this ticket'. The bank screen on the right becomes yours to use."
HAND_BACK = "When you're done, click 'Hand back to automation' and it carries on from where you left it."


def known_screen(code: str, rule: OutcomeRule, message: str, hints: dict[str, str]) -> list[str]:
    """For screens the app profile marks as needing a person (e.g. a one-time code)."""
    authored = [_fill(s, hints) for s in rule.instructions] or [
        f"The bank system is showing: {message}. Do what a teller would do to get past it."]
    return [TAKE, *authored, HAND_BACK]


def approval(cap: Capability, step: Step, params: dict[str, str], target_name: str) -> list[str]:
    """For an irreversible step: the person checks the request against the screen and
    performs the risky click themselves. The automation never clicks it."""
    request = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in params.items())
    return [
        TAKE,
        f"Check that the screen matches the request ({request}).",
        f"If it does, click '{target_name}' on the screen yourself. This can't be undone.",
        "Then click 'I clicked it, carry on', and the automation finishes the job.",
        "If anything looks wrong, click 'Don't do it' instead. Nothing is submitted.",
    ]


def approval_discovery(target_name: str, params: dict[str, str]) -> list[str]:
    """For an irreversible click the AI proposed during discovery."""
    request = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in params.items())
    return [
        TAKE,
        (f"The AI wants to click '{target_name}', which can't be undone. Check that the screen "
         f"matches the request ({request})."),
        f"If it does, click '{target_name}' on the screen yourself.",
        "Then click 'I clicked it, carry on'. The AI continues, and the step is recorded.",
        "If not, click 'Don't do it'. Nothing is submitted.",
    ]


def stuck(step: Step | None, params: dict[str, str], expected: str, observed: str) -> list[str]:
    """For a replay that cannot find its way and has no known recovery."""
    doing = substitute(step.intent, params).rstrip(".") if step else "sign in"
    return [
        TAKE,
        f"The automation was trying to: {doing}.",
        f"It expected {expected}, but the screen shows: {observed}.",
        ("Get the screen back to where that step can happen (for example close a pop-up or go "
         "back a page), or finish the job yourself."),
        "Then click 'Hand back to automation', or 'I finished it myself' if you completed it.",
    ]


def takeover(step: Step | None, params: dict[str, str], by: str) -> list[str]:
    """For a run that staff asked to take over. It paused before its next step."""
    doing = substitute(step.intent, params).rstrip(".") if step else "continue"
    return [
        TAKE,
        f"{by} asked to step in, so the request paused before its next step: {doing}.",
        "Do what you need on the screen.",
        ("Then click 'Hand back to automation' to let it carry on, 'I finished it myself' if "
         "you completed it, or 'Cancel the request' to stop it."),
    ]


def discovery(reason: str) -> list[str]:
    """For a discovery run (the AI exploring) that asked for help."""
    return [
        TAKE,
        f"The AI got stuck: {reason}.",
        "Look at the screen and fix what's in the way, or move it one step closer to the goal.",
        "Then click 'Hand back to automation'. The AI is told what you did and carries on.",
    ]


def _fill(text: str, hints: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: hints.get(m.group(1), m.group(0)), text)
