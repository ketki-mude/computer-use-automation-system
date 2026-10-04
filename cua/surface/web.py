"""Web surface adapter, built on Playwright.

Three calls make up the surface interface that the agent and the replay engine use:
  observe()          numbered outline of every frame, for the LLM
  resolve(target)    find one element from recorded strategies, for replay
  act(...)           click / fill / select / extract

Every action goes through `_act`, which asks the policy first. That is the single
choke point. A network route filter backs it up, so a JavaScript button that
navigates somewhere forbidden is stopped at the request level as well.
"""

import asyncio
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page, Route, async_playwright

from ..evidence import RunLog
from ..policy import Policy
from ..schema import Target

DOM_JS = (Path(__file__).parent / "dom.js").read_text()
PARAM = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")


class PolicyViolation(Exception):
    """The action is outside the allowlist."""


class ApprovalRequired(Exception):
    """The action is irreversible and nobody approved it."""


class NotInControl(Exception):
    """Automation tried to act while a human holds the session."""


@dataclass
class Observation:
    text: str  # numbered outline, one section per frame
    elements: dict[int, dict]  # ref -> element description, including its frame
    frames: dict[str, dict]  # frame name -> {title, url}

    def titles(self) -> dict[str, str]:
        return {name: f["title"] for name, f in self.frames.items() if f["title"]}


@dataclass
class Resolution:
    frame: Frame | None
    locator: Locator | None
    strategy_index: int  # which strategy matched; 0 is the primary
    element: dict | None
    attempts: list[tuple[str, int]] = field(default_factory=list)  # (strategy kind, match count)

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


class WebSurface:
    def __init__(self, policy: Policy, log: RunLog):
        self.policy = policy
        self.log = log
        self.page: Page | None = None
        self.blocked_requests: list[str] = []
        # Set by the session controller: raises NotInControl when a human holds the lease.
        self.lease_check: Callable[[], None] | None = None
        self._inflight = 0
        self._pw = None
        self._browser = None
        self.context = None
        self._tracing = False

    # ------------------------------------------------------------ lifecycle

    async def start(self, headed: bool = False, debug_port: int | None = None) -> None:
        """`debug_port` exposes the browser over CDP so an operator tool can attach to the
        same live session (localhost only)."""
        self._pw = await async_playwright().start()
        args = [f"--remote-debugging-port={debug_port}", "--remote-debugging-address=127.0.0.1"] \
            if debug_port else []
        self._browser = await self._pw.chromium.launch(headless=not headed, args=args)
        self.context = await self._browser.new_context(viewport={"width": 1280, "height": 820})
        await self.context.add_init_script(script=DOM_JS)
        await self.context.route("**/*", self._filter)
        self.page = await self.context.new_page()
        self.page.on("request", self._on_request)
        self.page.on("requestfinished", self._on_request_done)
        self.page.on("requestfailed", self._on_request_done)

    async def close(self) -> None:
        for closer in (self.context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except PlaywrightError:
                    pass
        if self._pw is not None:
            await self._pw.stop()

    async def goto(self, url: str) -> None:
        ok, why = self.policy.allows_url(url)
        if not ok:
            raise PolicyViolation(why)
        await self.page.goto(url)
        await self.settle()

    async def start_trace(self) -> None:
        # Started only after sign-on, so the trace never contains typed credentials.
        await self.context.tracing.start(screenshots=True, snapshots=True)
        self._tracing = True

    async def stop_trace(self, path: Path | None) -> None:
        if self._tracing:
            await self.context.tracing.stop(path=str(path) if path else None)
            self._tracing = False

    # ------------------------------------------------------------ network allowlist

    async def _filter(self, route: Route) -> None:
        url = route.request.url
        ok, why = self.policy.allows_url(url)
        if ok:
            await route.continue_()
            return
        self.blocked_requests.append(url)
        self.log.event("policy_blocked", f"network request refused: {why}", url=url)
        await route.abort("blockedbyclient")

    def _on_request(self, _request) -> None:
        self._inflight += 1

    def _on_request_done(self, _request) -> None:
        self._inflight = max(0, self._inflight - 1)

    async def settle(self, timeout: float = 8.0) -> None:
        """Wait until no request is in flight and every frame has finished loading."""
        await asyncio.sleep(0.2)  # let a click-triggered navigation begin
        deadline = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < deadline:
            busy = self._inflight > 0 or not await self._all_frames_complete()
            if busy:
                quiet_since = None
            else:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= 0.25:
                    return
            await asyncio.sleep(0.05)

    async def _all_frames_complete(self) -> bool:
        for f in self.frames():
            try:
                if await f.evaluate("document.readyState") != "complete":
                    return False
            except PlaywrightError:
                return False
        return True

    # ------------------------------------------------------------ frames

    def frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def frame(self, name: str | None) -> Frame | None:
        if name is None:
            return self.page.main_frame
        return next((f for f in self.frames() if f.name == name), None)

    async def _ready(self, frame: Frame) -> bool:
        try:
            if not await frame.evaluate("() => !!window.__cua"):
                await frame.add_script_tag(content=DOM_JS)
            return True
        except PlaywrightError:
            return False

    # ------------------------------------------------------------ observe

    async def observe(self) -> Observation:
        await self.settle()
        ref, parts, elements, frames = 1, [], {}, {}
        for frame in self.frames():
            if not await self._ready(frame):
                continue
            try:
                data = await frame.evaluate("(start) => window.__cua.observe(start)", ref)
            except PlaywrightError:
                continue  # the frame navigated while being read
            ref = data["next_ref"]
            name = frame.name or "top"
            frames[name] = {"title": data["title"], "url": data["url"]}
            if not data["lines"]:
                continue
            parts.append(f'## frame "{name}" · title "{data["title"]}" · {urlparse(data["url"]).path}')
            parts.extend(data["lines"])
            for el in data["elements"]:
                el["frame"] = frame.name or None
                el["frame_url"] = data["url"]
                elements[el["ref"]] = el
        return Observation("\n".join(parts), elements, frames)

    async def screens(self) -> dict[str, dict]:
        """Visible text and title of every frame, for detectors and checkpoints."""
        out = {}
        for frame in self.frames():
            if not await self._ready(frame):
                continue
            try:
                out[frame.name or "top"] = {
                    "text": await frame.evaluate("() => window.__cua.text()"),
                    "title": await frame.evaluate("() => window.__cua.title()"),
                    "url": frame.url,
                }
            except PlaywrightError:
                continue
        return out

    # ------------------------------------------------------------ resolve

    async def resolve(self, target: Target, params: dict[str, str]) -> Resolution:
        frame = self.frame(target.frame)
        if frame is None or not await self._ready(frame):
            return Resolution(None, None, -1, None, [("frame:" + str(target.frame), 0)])
        attempts = []
        for i, strategy in enumerate(target.strategies):
            spec = substitute(strategy.model_dump(), params)
            token = secrets.token_hex(4)
            try:
                data = await frame.evaluate("([s, t]) => window.__cua.resolve(s, t)", [spec, token])
            except PlaywrightError:
                attempts.append((strategy.kind, 0))
                continue
            attempts.append((strategy.kind, data["count"]))
            if data["count"] == 1:
                return Resolution(frame, frame.locator(f'[data-cua-hit="{token}"]'), i,
                                  data["element"], attempts)
        return Resolution(None, None, -1, None, attempts)

    async def check_strategy(self, frame_name: str | None, strategy: dict, ref: int) -> bool:
        """True if the strategy matches exactly one element and it is element [ref].
        Used at record time to keep only locators proven on the live screen."""
        frame = self.frame(frame_name)
        if frame is None:
            return False
        try:
            data = await frame.evaluate("([s, t]) => window.__cua.resolve(s, t)",
                                        [strategy, secrets.token_hex(4)])
        except PlaywrightError:
            return False
        return data["count"] == 1 and data["refs"][0] == str(ref)

    # ------------------------------------------------------------ act

    async def act_ref(self, obs: Observation, ref: int, action: str, value: str | None = None,
                      allow_irreversible: bool = False) -> ActResult:
        el = obs.elements.get(ref)
        if el is None:
            raise LookupError(f"there is no element [{ref}] on the current screen")
        frame = self.frame(el["frame"])
        if frame is None:
            raise LookupError(f"frame {el['frame']!r} is gone")
        return await self._act(frame, frame.locator(f'[data-cua-ref="{ref}"]'), el, action, value,
                               allow_irreversible)

    async def act(self, res: Resolution, action: str, value: str | None = None,
                  allow_irreversible: bool = False) -> ActResult:
        return await self._act(res.frame, res.locator, res.element, action, value, allow_irreversible)

    async def _act(self, frame: Frame, locator: Locator, el: dict, action: str, value: str | None,
                   allow_irreversible: bool) -> ActResult:
        if self.lease_check is not None:
            self.lease_check()
        decision = self.policy.authorize(action, el, frame.url)
        self.log.event("policy_decision", "", action=action, element=describe(el),
                       allowed=decision.allowed, risk=decision.risk, reason=decision.reason)
        if not decision.allowed:
            raise PolicyViolation(decision.reason)
        if decision.risk == "irreversible" and not allow_irreversible:
            raise ApprovalRequired(f"{describe(el)} is irreversible and needs explicit approval")
        text = None
        if action == "click":
            await locator.click(timeout=5000)
        elif action == "fill":
            await locator.fill(value or "", timeout=5000)
        elif action == "select":
            await self._select(locator, value or "")
        elif action == "extract":
            if el.get("tag") in ("input", "textarea", "select"):
                text = await locator.input_value(timeout=5000)
            else:
                text = (await locator.inner_text(timeout=5000)).strip()
        else:
            raise PolicyViolation(f"unknown action {action}")
        await self.settle()
        return ActResult(decision.risk, text)

    async def _select(self, locator: Locator, wanted: str) -> None:
        """Pick the option whose text equals `wanted`, else the single one containing it.
        Option labels often carry data ("CHECKING ($820.40 avail)"), so exact text is brittle."""
        options = await locator.evaluate("s => Array.from(s.options).map(o => o.text.trim())")
        picks = [o for o in options if o == wanted] or \
            [o for o in options if wanted.lower() in o.lower()]
        if len(picks) != 1:
            raise LookupError(f"option {wanted!r} matches {len(picks)} of {options}")
        await locator.select_option(label=picks[0], timeout=5000)

    # ------------------------------------------------------------ evidence

    async def screenshot(self, path: Path, sensitive: list[str]) -> Path | None:
        """Full-page screenshot with every element showing a sensitive value blacked out."""
        masks = []
        for frame in self.frames():
            if not await self._ready(frame):
                continue
            try:
                if await frame.evaluate("(v) => window.__cua.markSensitive(v)", sensitive):
                    masks.append(frame.locator("[data-cua-mask]"))
            except PlaywrightError:
                continue
        try:
            await self.page.screenshot(path=str(path), mask=masks, mask_color="#111111")
            return path
        except PlaywrightError:
            return None

    async def dom_snapshot(self) -> str:
        parts = []
        for frame in self.frames():
            try:
                parts.append(f"<!-- frame {frame.name or 'top'} {frame.url} -->\n{await frame.content()}")
            except PlaywrightError:
                continue
        return "\n\n".join(parts)


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

