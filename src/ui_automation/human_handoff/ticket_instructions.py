"""Step-by-step instructions for the person who takes a ticket.

A ticket is only useful if someone who has never seen the run can act on it, so every
ticket says what happened, what to check, what to do, and which button ends it.
"""

import re

from ..catalog.capability_review import target_text
from ..models.capability import Capability, OutcomeRule, Step
from ..screen.screen_interface import substitute

TAKE = "Click 'Take this ticket'. The bank screen on the right becomes yours to use."
TAKE_TO_REVIEW = "Click 'Take this ticket', so nobody else handles it at the same time."
HAND_BACK = "When you're done, click 'Hand back to automation' and it carries on from where you left it."


def known_screen(code: str, rule: OutcomeRule, message: str, hints: dict[str, str]) -> list[str]:
    """For screens the app profile marks as needing a person (e.g. a one-time code)."""
    authored = [_fill(s, hints) for s in rule.instructions] or [
        f"The bank system is showing: {message}. Do what a teller would do to get past it."]
    return [TAKE, *authored, HAND_BACK]


def approval(cap: Capability, step: Step, params: dict[str, str], target_name: str) -> list[str]:
    """For an irreversible step: the person checks the paused screen against the request and
    decides. The screen is view only; on approval the automation makes the click."""
    return [
        TAKE_TO_REVIEW,
        f"Check that the screen shows what was asked: {_request(params)}.",
        f"If it does, click 'Approve'. The automation then clicks '{target_name}'. This can't be undone.",
        "If anything looks wrong, click 'Don't do it'. Nothing is submitted.",
    ]


def approval_discovery(target_name: str, params: dict[str, str]) -> list[str]:
    """For an irreversible click the AI proposed during discovery."""
    return [
        TAKE_TO_REVIEW,
        (f"The AI wants to click '{target_name}', which can't be undone. Check that the screen "
         f"shows what was asked: {_request(params)}."),
        f"If it does, click 'Approve'. The AI then clicks '{target_name}', and the step is recorded.",
        "If not, click 'Don't do it'. Nothing is submitted.",
    ]


def problem(kind: str, step: Step | None) -> str:
    """Why a replay stopped, in words for the person taking the ticket (the error kind and
    locator details stay in the run log)."""
    target = target_text(step.target) if step else "the sign-on screen"
    return {
        "TARGET_NOT_FOUND": f"It couldn't find {target} on the screen",
        "TARGET_AMBIGUOUS": f"More than one thing on the screen matched {target}",
        "CHECKPOINT_FAILED": "After that step, the screen didn't show what it expected",
        "UNKNOWN_STATE": "It didn't recognise the screen it was on",
        "SURFACE_ERROR": "The browser had a problem showing the screen",
    }.get(kind, "Something on the screen wasn't as expected")


def stuck(step: Step | None, params: dict[str, str], problem: str, screen: str) -> list[str]:
    """For a replay that cannot find its way and has no known recovery. `problem` is what went
    wrong in plain words; `screen` is the title of the screen it is on."""
    doing = substitute(step.intent, params).rstrip(".") if step else "sign in"
    return [
        TAKE,
        f"It was trying to {doing[:1].lower() + doing[1:]}. {problem}. The screen shows: {screen}.",
        ("To let it carry on: get the screen back to where that step can happen (close a pop-up, "
         "or go back a page), then click 'Hand back to automation'."),
        ("Or finish the job yourself and click 'I finished it myself', or click 'Cancel the "
         "request' if it shouldn't go ahead."),
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


def _request(params: dict[str, str]) -> str:
    return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in params.items())


def _fill(text: str, hints: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: hints.get(m.group(1), m.group(0)), text)
