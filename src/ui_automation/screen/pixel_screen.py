"""The Screen interface with no DOM: perception is OCR of screenshots, actions are mouse and keys.

This is how the system reaches a surface that has no DOM: a native desktop app, or one streamed
through Citrix or remote desktop. The capability format and the replay engine do not change;
only how a target is found and acted on does:

  role_name / text   the words on screen, e.g. the button whose caption is "Search"
  label_anchor       the input box right of a label ("Member Number:"), found in the pixels,
                     or for a value, the next text to the right of the label
  table_row / _cell  a column's header gives its x-band; the key value gives the row's line
  css                a web-only locator: nothing to match on pixels, so the next one is tried

Coordinates are never stored. They are computed from the current screenshot at the moment of
each action, relative to the app's window and divided by the display scale, so the same
capability replays at another window size or on a high-DPI display.

The window comes from a WindowHost. Here that is a browser window used only as a window: no DOM
helper is loaded and nothing is read from the page. A desktop host would implement the same few
methods with OS screen capture and input (for example mss and pywinauto), and nothing above it
would change. The host also keeps what belongs to the window rather than the screen: the
network allowlist and the native-dialog rules.

Replay only: discovery on a pixel surface (a model reading screenshots) is not built.
"""

import asyncio
import functools
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ..evidence_writer import RunLog
from ..models.app_profile import PixelRegion
from ..models.capability import Target
from ..safety.safety_policy import SafetyPolicy
from ..settings import (
    FIELD_CLICK_INSET_PT,
    FIELD_SEARCH_WIDTH_PT,
    LIVE_VIEW_JPEG_QUALITY,
    OCR_MIN_CONFIDENCE,
    PAGE_SETTLE_TIMEOUT_S,
    PIXEL_POLL_INTERVAL_S,
    PIXEL_QUIET_PERIOD_S,
    PIXEL_SCALE,
    SCREEN_CAPTURE_TIMEOUT_MS,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)
from .browser_screen import BrowserScreen
from .screen_interface import (
    ActResult,
    ApprovalRequired,
    Observation,
    PolicyViolation,
    Resolution,
    ScreenError,
    describe,
    substitute,
)

INPUT_ROLES = {"textbox", "combobox", "listbox"}
BORDER_MAX_GRAY = 170  # an input box's border is darker than this...
FIELD_MIN_GRAY = 235  # ...and its inside lighter
COLUMN_PAD_PT = 4  # a table column starts this far left of its header text
SELECT_ALL = "ControlOrMeta+a"
MASK_BGR = (17, 17, 17)
MASK_PAD_PX = 2


@dataclass(frozen=True)
class PixelSurface:
    """How a pixel replay sets up its window: size in points and display scale."""
    window: tuple[int, int] = (WINDOW_WIDTH, WINDOW_HEIGHT)
    scale: float = PIXEL_SCALE


@dataclass(frozen=True)
class TextBox:
    """A piece of text the OCR read, with its box in window points."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def on_line_of(self, other: "TextBox") -> bool:
        return abs(self.cy - other.cy) < 0.6 * max(self.y1 - self.y0, other.y1 - other.y0)


@dataclass(frozen=True)
class Hit:
    """Where to act (window points) and the text there."""
    box: TextBox
    x: float
    y: float
    text: str


def norm(text: str) -> str:
    return " ".join(text.split())


def same_label(text: str, label: str) -> bool:
    return norm(text).rstrip(":").strip() == norm(label).rstrip(":").strip()


@functools.cache
def ocr_engine() -> Any:
    try:
        from rapidocr import RapidOCR
    except ImportError as e:
        raise ScreenError('the pixel surface needs OCR: pip install -e ".[pixel]"') from e
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    return RapidOCR()


def _cv2() -> Any:
    import cv2  # installed with the OCR engine

    return cv2


# ---------------------------------------------------------------- where the window lives

class WindowHost(Protocol):
    """What a pixel surface needs from the place the app's window lives."""
    scale: float  # screenshot pixels per window point
    dialogs: list[dict]
    dialog_rules: dict

    async def start(self, headed: bool, slow_mo: int) -> None: ...
    async def open(self, address: str) -> None: ...  # the app's entry point
    async def capture(self) -> bytes: ...  # a PNG of the window
    async def capture_jpeg(self, quality: int) -> bytes | None: ...
    async def click(self, x: float, y: float) -> None: ...  # window points
    async def type(self, text: str) -> None: ...
    async def press(self, key: str) -> None: ...
    async def back(self) -> None: ...
    async def reload(self) -> None: ...
    async def cookie(self, name: str) -> str | None: ...
    async def bring_to_front(self) -> None: ...
    async def close(self) -> None: ...


class BrowserWindowHost:
    """A browser window used only as a window: it opens the app's address, takes screenshots,
    and sends mouse and keys. No DOM helper is loaded and nothing is read from the page."""

    def __init__(self, policy: SafetyPolicy, log: RunLog, surface: PixelSurface):
        self._browser = BrowserScreen(policy, log)  # for the window, allowlist and dialogs only
        self.surface = surface
        self.scale = surface.scale

    @property
    def dialogs(self) -> list[dict]:
        return self._browser.dialogs

    @property
    def dialog_rules(self) -> dict:
        return self._browser.dialog_rules

    @dialog_rules.setter
    def dialog_rules(self, rules: dict) -> None:
        self._browser.dialog_rules = rules

    async def start(self, headed: bool, slow_mo: int) -> None:
        await self._browser.start(headed=headed, slow_mo=slow_mo, inspector=False,
                                  window=self.surface.window, scale=self.surface.scale)

    async def _page(self, call: str, *args, **kwargs) -> Any:
        from playwright.async_api import Error as PlaywrightError

        try:
            target = self._browser.page
            for part in call.split("."):
                target = getattr(target, part)
            return await target(*args, **kwargs)
        except PlaywrightError as e:
            raise ScreenError(str(e).splitlines()[0]) from e

    async def open(self, address: str) -> None:
        await self._page("goto", address)

    async def capture(self) -> bytes:
        return await self._page("screenshot", caret="hide", timeout=SCREEN_CAPTURE_TIMEOUT_MS)

    async def capture_jpeg(self, quality: int) -> bytes | None:
        try:
            return await self._page("screenshot", type="jpeg", quality=quality, scale="css")
        except ScreenError:
            return None

    async def click(self, x: float, y: float) -> None:
        await self._page("mouse.click", x, y)

    async def type(self, text: str) -> None:
        await self._page("keyboard.type", text)

    async def press(self, key: str) -> None:
        await self._page("keyboard.press", key)

    async def back(self) -> None:
        await self._page("go_back")

    async def reload(self) -> None:
        await self._page("reload")

    async def cookie(self, name: str) -> str | None:
        return await self._browser.cookie(name)

    async def bring_to_front(self) -> None:
        await self._browser.bring_to_front()

    async def close(self) -> None:
        await self._browser.close()


# ---------------------------------------------------------------- the screen, from pixels

class PixelScreen:
    """The Screen interface from screenshots and mouse only (replay; see the module docstring)."""

    snapshot_name = "screen-text.txt"  # there is no DOM: the failure record is the screen's text

    def __init__(self, policy: SafetyPolicy, log: RunLog, regions: dict[str, PixelRegion],
                 surface: PixelSurface | None = None, host: WindowHost | None = None):
        self.policy = policy
        self.log = log
        self.regions = regions
        self.host: WindowHost = host or BrowserWindowHost(policy, log, surface or PixelSurface())
        self.page: WindowHost | None = None  # set once the window is open
        self.lease_check = None
        self.debug_port: int | None = None
        self._on_human = None
        self._last: tuple[str, list[TextBox], np.ndarray] | None = None

    @property
    def dialogs(self) -> list[dict]:
        return self.host.dialogs

    @property
    def dialog_rules(self) -> dict:
        return self.host.dialog_rules

    @dialog_rules.setter
    def dialog_rules(self, rules: dict) -> None:
        self.host.dialog_rules = rules

    # ------------------------------------------------------------ lifecycle

    async def start(self, headed: bool = False, debug_port: int | None = None,
                    slow_mo: int = 0) -> None:
        await self.host.start(headed, slow_mo)
        self.page = self.host

    async def close(self) -> None:
        await self.host.close()

    async def goto(self, url: str) -> None:
        ok, why = self.policy.allows_url(url)
        if not ok:
            raise PolicyViolation(why)
        await self.host.open(url)
        await self.settle()

    async def go_back(self, frame_name: str | None) -> None:
        await self.host.back()
        await self.settle()

    async def reload(self, frame_name: str | None) -> None:
        await self.host.reload()
        await self.settle()

    async def cookie(self, name: str) -> str | None:
        return await self.host.cookie(name)

    async def start_trace(self) -> None:
        """No trace: a browser trace records DOM snapshots, which this surface never reads."""

    async def stop_trace(self, path: Path | None) -> None:
        """See start_trace."""

    # ------------------------------------------------------------ reading the screen

    async def _read(self) -> tuple[list[TextBox], np.ndarray]:
        """OCR the window (cached while the screen is unchanged). Returns text boxes in window
        points, and the grey image for finding input boxes."""
        png = await self.host.capture()
        digest = hashlib.sha1(png).hexdigest()
        if self._last and self._last[0] == digest:
            return self._last[1], self._last[2]
        cv2 = _cv2()
        image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        result = await asyncio.to_thread(ocr_engine(), image)
        boxes = []
        for polygon, text, score in zip(result.boxes if result.boxes is not None else [],
                                        result.txts or [], result.scores or [], strict=False):
            if score < OCR_MIN_CONFIDENCE or not text.strip():
                continue
            xs, ys = [float(p[0]) for p in polygon], [float(p[1]) for p in polygon]
            s = self.host.scale
            boxes.append(TextBox(norm(text), min(xs) / s, min(ys) / s, max(xs) / s, max(ys) / s))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        self._last = (digest, boxes, gray)
        return boxes, gray

    def _size(self, gray: np.ndarray) -> tuple[float, float]:
        return gray.shape[1] / self.host.scale, gray.shape[0] / self.host.scale

    def _pane(self, boxes: list[TextBox], name: str | None,
              size: tuple[float, float]) -> list[TextBox] | None:
        """The boxes in a pane, or None when the pane is not on screen. A missing name (or one
        the app profile does not define) means the whole window."""
        region = self.regions.get(name) if name else None
        if region is None:
            return boxes
        if region.visible_when and not any(region.visible_when in b.text for b in boxes):
            return None
        x1 = region.x1 if region.x1 is not None else size[0]
        y1 = region.y1 if region.y1 is not None else size[1]
        return [b for b in boxes if region.x0 <= b.cx < x1 and region.y0 <= b.cy < y1]

    @staticmethod
    def _lines(boxes: list[TextBox]) -> list[list[TextBox]]:
        lines: list[list[TextBox]] = []
        for b in sorted(boxes, key=lambda b: (b.cy, b.x0)):
            if lines and b.on_line_of(lines[-1][0]):
                lines[-1].append(b)
            else:
                lines.append([b])
        return [sorted(line, key=lambda b: b.x0) for line in lines]

    def _text(self, boxes: list[TextBox]) -> str:
        return "\n".join(" ".join(b.text for b in line) for line in self._lines(boxes))

    async def frame_texts(self) -> dict[str, dict]:
        """Text of each pane on screen, plus the whole window ("top"), for detectors."""
        boxes, gray = await self._read()
        size = self._size(gray)
        out = {}
        for name in self.regions:
            pane = self._pane(boxes, name, size)
            if pane is not None:
                text = self._text(pane)
                out[name] = {"text": text, "title": text, "url": ""}
        whole = self._text(boxes)
        out.setdefault("top", {"text": whole, "title": whole, "url": ""})
        return out

    async def observe(self) -> Observation:
        """The screen as text lines, so personal data next to its label can be registered."""
        boxes, _ = await self._read()
        elements, lines_out, ref = {}, [], 0
        for line in self._lines(boxes):
            row = [b.text for b in line]
            cells = []
            for pos, b in enumerate(line):
                ref += 1
                elements[ref] = {"kind": "cell", "tag": "screen-text", "role": "text", "text": b.text,
                                 "row_texts": row, "pos": pos, "frame": None}
                cells.append(f"[{ref}]{b.text}")
            lines_out.append(" | ".join(cells))
        return Observation("\n".join(lines_out), elements, {})

    # ------------------------------------------------------------ finding a target

    async def resolve(self, target: Target, params: dict[str, str]) -> Resolution:
        boxes, gray = await self._read()
        pool = self._pane(boxes, target.frame, self._size(gray))
        if pool is None:
            return Resolution(None, None, -1, None, [(f"pane:{target.frame}", 0)])
        attempts = []
        for i, strategy in enumerate(target.strategies):
            spec = substitute(strategy.model_dump(), params)
            hits = self._find(spec, pool, gray)
            attempts.append((spec["kind"], len(hits)))
            if len(hits) == 1:
                return Resolution(target.frame, hits[0], i, self._element(spec, hits[0], target.frame),
                                  attempts)
        return Resolution(None, None, -1, None, attempts)

    @staticmethod
    def _element(spec: dict, hit: Hit, frame: str | None) -> dict:
        is_value = spec["kind"] == "table_cell" or (spec["kind"] == "label_anchor"
                                                    and spec["role"] not in INPUT_ROLES)
        role = "cell" if is_value else spec.get("role") or "text"
        return {"surface": "pixel", "tag": "screen-text", "role": role,
                "name": "" if is_value else hit.text, "label": spec.get("label", ""),
                "column": spec.get("column") or spec.get("label", ""), "frame": frame,
                "bbox": [round(hit.box.x0), round(hit.box.y0), round(hit.box.x1), round(hit.box.y1)]}

    def _find(self, spec: dict, pool: list[TextBox], gray: np.ndarray) -> list[Hit]:
        kind = spec["kind"]
        if kind == "role_name":
            return self._words(pool, spec["name"])
        if kind == "text":
            return self._words(pool, spec["text"])
        if kind == "label_anchor":
            hits = []
            for label in (b for b in pool if same_label(b.text, spec["label"])):
                if spec["role"] in INPUT_ROLES:
                    if (x := self._field_right_of(label, gray)) is not None:
                        hits.append(Hit(label, x, label.cy, ""))
                else:  # a value: the next text to the right on the same line
                    right = sorted((b for b in pool if b.on_line_of(label) and b.x0 > label.x1),
                                   key=lambda b: b.x0)
                    if right:
                        hits.append(Hit(right[0], right[0].cx, right[0].cy, right[0].text))
            return hits
        if kind in ("table_row", "table_cell"):
            return self._table(spec, pool)
        return []  # css: a web locator; there is nothing to match on pixels

    @staticmethod
    def _words(pool: list[TextBox], wanted: str) -> list[Hit]:
        """Text equal to `wanted`; failing that, `wanted` as whole words inside a longer text
        (the OCR sometimes reads neighbouring captions as one box)."""
        wanted = norm(wanted)
        exact = [Hit(b, b.cx, b.cy, b.text) for b in pool if b.text == wanted]
        if exact:
            return exact
        hits = []
        for b in pool:
            padded = f" {b.text} "
            at = padded.find(f" {wanted} ")
            if at >= 0:  # estimate where those characters sit inside the box
                middle = (at + len(wanted) / 2) / len(b.text)
                hits.append(Hit(b, b.x0 + (b.x1 - b.x0) * middle, b.cy, wanted))
        return hits

    def _field_right_of(self, label: TextBox, gray: np.ndarray) -> float | None:
        """The left edge (window points) of the input box right of a label: a dark border with
        a light inside, along the label's centre line."""
        s = self.host.scale
        row = gray[int(label.cy * s)]
        start = int(label.x1 * s) + 2
        end = min(len(row) - int(3 * s), int((label.x1 + FIELD_SEARCH_WIDTH_PT) * s))
        inside = slice(int(1 * s), int(2.5 * s) + 1)
        for x in range(start, end):
            if row[x] < BORDER_MAX_GRAY and (row[x + inside.start:x + inside.stop] > FIELD_MIN_GRAY).all():
                return x / s + FIELD_CLICK_INSET_PT
        return None

    def _table(self, spec: dict, pool: list[TextBox]) -> list[Hit]:
        lines = self._lines(pool)
        key_column, key_value = norm(spec["row"]["column"]), norm(spec["row"]["equals"])
        for li, line in enumerate(lines):
            if not any(b.text == key_column for b in line):
                continue
            bands = self._bands(line)
            want = bands.get(norm(spec["column"])) if spec["kind"] == "table_cell" else None
            hits = []
            for row in lines[li + 1:]:
                if not any(b.text == key_value and _in(b, bands[key_column]) for b in row):
                    continue
                if spec["kind"] == "table_cell":
                    hits += [Hit(b, b.cx, b.cy, b.text) for b in row if want and _in(b, want)]
                else:
                    hits += [Hit(b, b.cx, b.cy, b.text) for b in row if b.text == norm(spec["name"])]
            return hits  # the first header line with that column is the table
        return []

    @staticmethod
    def _bands(header: list[TextBox]) -> dict[str, tuple[float, float]]:
        """Each column's x-range: from its header text to the next header (header text is left
        aligned in these tables; values may be right aligned under it)."""
        bands = {}
        for i, b in enumerate(header):
            right = header[i + 1].x0 - COLUMN_PAD_PT if i + 1 < len(header) else float("inf")
            bands[b.text] = (b.x0 - COLUMN_PAD_PT, right)
        return bands

    # ------------------------------------------------------------ acting

    async def act(self, res: Resolution, action: str, value: str | None = None,
                  allow_irreversible: bool = False) -> ActResult:
        if self.lease_check is not None:
            self.lease_check()
        el, hit = res.element, res.locator
        decision = self.policy.authorize(action, el, res.frame_url)
        self.log.event("policy_decision", "", action=action, element=describe(el),
                       allowed=decision.allowed, risk=decision.risk, reason=decision.reason)
        if not decision.allowed:
            raise PolicyViolation(decision.reason)
        if decision.risk == "irreversible" and not allow_irreversible:
            raise ApprovalRequired(f"{describe(el)} is irreversible and needs explicit approval")
        text = None
        if action == "click":
            await self.host.click(hit.x, hit.y)
        elif action in ("fill", "select"):
            await self.host.click(hit.x, hit.y)
            if action == "fill":
                await self.host.press(SELECT_ALL)
                await self.host.press("Backspace")
            await self.host.type(value or "")
            if action == "select":  # type-ahead picks the option; Enter commits it
                await self.host.press("Enter")
        elif action == "extract":
            text = hit.text
        else:
            raise PolicyViolation(f"unknown action {action}")
        if action != "extract":
            self.log.event("pixel_action", f"{action} at ({hit.x:.0f}, {hit.y:.0f}) on {describe(el)}",
                           x=round(hit.x), y=round(hit.y))
        await self.settle()
        return ActResult(decision.risk, text)

    async def act_ref(self, obs: Observation, ref: int, action: str, value: str | None = None,
                      allow_irreversible: bool = False) -> ActResult:
        raise ScreenError("discovery on the pixel surface is not built; it replays capabilities")

    async def check_strategy(self, frame_name: str | None, strategy: dict, ref: int) -> bool:
        raise ScreenError("discovery on the pixel surface is not built; it replays capabilities")

    async def settle(self, timeout: float = PAGE_SETTLE_TIMEOUT_S) -> None:
        """Wait until the screen stops changing for PIXEL_QUIET_PERIOD_S (a condition on the
        pixels themselves, not a fixed pause)."""
        deadline = time.monotonic() + timeout
        last, since = None, time.monotonic()
        while time.monotonic() < deadline:
            try:
                digest = hashlib.sha1(await self.host.capture()).hexdigest()
            except ScreenError:  # mid-navigation the window cannot be captured: still changing
                digest = None
            now = time.monotonic()
            if digest is None or digest != last:
                last, since = digest, now
            elif now - since >= PIXEL_QUIET_PERIOD_S:
                return
            await asyncio.sleep(PIXEL_POLL_INTERVAL_S)

    # ------------------------------------------------------------ evidence

    async def screenshot(self, path: Path, sensitive: list[str]) -> Path | None:
        """The window, with every piece of text the redactor would mask blacked out."""
        cv2 = _cv2()
        png = await self.host.capture()
        boxes, _ = await self._read()
        image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        s = self.host.scale
        for b in boxes:
            if self.log.redactor.text(b.text) != b.text or any(v and v in b.text for v in sensitive):
                cv2.rectangle(image, (int(b.x0 * s) - MASK_PAD_PX, int(b.y0 * s) - MASK_PAD_PX),
                              (int(b.x1 * s) + MASK_PAD_PX, int(b.y1 * s) + MASK_PAD_PX), MASK_BGR, -1)
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            return None
        path.write_bytes(encoded.tobytes())
        return path

    async def dom_snapshot(self) -> str:
        boxes, _ = await self._read()
        return "Pixel surface: there is no DOM. Text read from the screen:\n\n" + self._text(boxes)

    async def capture_frame(self) -> bytes | None:
        return await self.host.capture_jpeg(LIVE_VIEW_JPEG_QUALITY)

    # ------------------------------------------------------------ the person's input (handoff)

    async def install_operator_recorder(self, on_event: Any, script: str) -> None:
        """No page script here: the person's clicks and typing arrive through click_at and
        type_text, and are recorded from there."""
        self._on_human = on_event

    async def click_at(self, x: float, y: float) -> None:
        await self.host.click(x, y)
        if self._on_human and self._last:
            under = next((b.text for b in self._last[1] if b.x0 <= x <= b.x1 and b.y0 <= y <= b.y1), "")
            self._on_human(None, {"type": "click", "target": {"role": "text", "name": under}})

    async def type_text(self, text: str) -> None:
        await self.host.type(text)
        if self._on_human:
            self._on_human(None, {"type": "fill", "target": {}, "value": "•" * len(text)})

    async def press_key(self, key: str) -> None:
        await self.host.press(key)

    async def bring_to_front(self) -> None:
        await self.host.bring_to_front()


def _in(box: TextBox, band: tuple[float, float]) -> bool:
    return band[0] <= box.cx < band[1]
