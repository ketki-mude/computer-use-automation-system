"""The screen interface: observe, resolve, act. It has no Playwright dependency.

This is the seam between "how we perceive and act on a screen" and "the recorded flow".
Recorded strategies say what to find in human terms; an adapter decides how.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

PARAM = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")

class PolicyViolation(Exception):
    """The action is outside the allowlist."""

class ApprovalRequired(Exception):
    """The action is irreversible and nobody approved it."""

class NotInControl(Exception):
    """Automation tried to act while a human holds the session."""


class ScreenError(Exception):
    """The app or browser failed while performing an action."""

@dataclass
class Observation:
    text: str  # numbered outline, one section per frame
    elements: dict[int, dict]  # ref -> element description, including its frame
    frames: dict[str, dict]  # frame name -> {title, url}

    def titles(self) -> dict[str, str]:
        return {name: f["title"] for name, f in self.frames.items() if f["title"]}

@dataclass
class Resolution:
    frame: Any  # the adapter's own handles; opaque to the rest of the system
    locator: Any
    strategy_index: int  # which strategy matched; 0 is the primary
    element: dict | None
    attempts: list[tuple[str, int]] = field(default_factory=list)  # (strategy kind, match count)
    frame_url: str = ""  # where the matched element lives, for policy checks

    @property
    def found(self) -> bool:
        return self.locator is not None

    @property
    def ambiguous(self) -> bool:
        return not self.found and any(n > 1 for _, n in self.attempts)

@dataclass
class ActResult:
    risk: str
    text: str | None = None  # extract only

def substitute(obj, params: dict[str, str]):
    """Replace ${param} references in every string of a strategy or condition."""
    if isinstance(obj, str):
        return PARAM.sub(lambda m: str(params.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: substitute(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, params) for v in obj]
    return obj

def describe(el: dict) -> str:
    """A human-readable element description for logs: role "name" (label "x")."""
    s = el.get("role") or el.get("tag", "?")
    if el.get("name"):
        s += f' "{el["name"][:40]}"'
    if el.get("label"):
        s += f' (label "{el["label"]}")'
    if el.get("kind") == "cell" or el.get("role") == "cell":
        # A cell's text is data (a balance, a name); describe it by where it is instead.
        s = f'cell [{el.get("column") or (el.get("row_texts") or ["?"])[max(el.get("pos", 1) - 1, 0)]}]'
    return s


class Screen(Protocol):
    """What discovery, replay and the handoff need from any screen: a web browser today,
    a desktop app (UI Automation / AX tree) or a pixel-only screen (OCR) later. Nothing
    outside the adapter knows how elements are found or clicked."""

    policy: Any
    dialogs: list[dict]
    dialog_rules: dict
    lease_check: Any
    debug_port: int | None
    snapshot_name: str  # file name for dom_snapshot(): the page's DOM, or its text if it has none

    async def goto(self, url: str) -> None: ...
    async def observe(self) -> Observation: ...
    async def frame_texts(self) -> dict[str, dict]: ...
    async def resolve(self, target: Any, params: dict[str, str]) -> Resolution: ...
    async def check_strategy(self, frame_name: str | None, strategy: dict, ref: int) -> bool: ...
    async def act_ref(self, obs: Observation, ref: int, action: str, value: str | None = None,
                      allow_irreversible: bool = False) -> ActResult: ...
    async def act(self, res: Resolution, action: str, value: str | None = None,
                  allow_irreversible: bool = False) -> ActResult: ...
    async def settle(self) -> None: ...
    async def go_back(self, frame_name: str | None) -> None: ...
    async def reload(self, frame_name: str | None) -> None: ...
    async def cookie(self, name: str) -> str | None: ...
    async def screenshot(self, path: Path, sensitive: list[str]) -> Path | None: ...
    async def dom_snapshot(self) -> str: ...
    async def start_trace(self) -> None: ...
    async def stop_trace(self, path: Path | None) -> None: ...
    async def capture_frame(self) -> bytes | None: ...
    async def install_operator_recorder(self, on_event: Any, script: str) -> None: ...
    async def click_at(self, x: float, y: float) -> None: ...
    async def type_text(self, text: str) -> None: ...
    async def press_key(self, key: str) -> None: ...
    async def bring_to_front(self) -> None: ...
