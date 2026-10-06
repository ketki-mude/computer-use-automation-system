"""Web screen adapter, built on Playwright.

Three calls make up the screen interface that the agent and the replay engine use:
  observe()          numbered outline of every frame, for the LLM
  resolve(target)    find one element from recorded strategies, for replay
  act(...)           click / fill / select / extract

Every action goes through `_act`, which asks the policy first. That is the single
choke point. A network route filter backs it up, so a JavaScript button that
navigates somewhere forbidden is stopped at the request level as well.
"""

import asyncio
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page, Route, async_playwright

from ..evidence_writer import RunLog
from ..models.capability import Target
from ..safety.safety_policy import SafetyPolicy
from ..settings import (
    ACTION_TIMEOUT_MS,
    LIVE_VIEW_JPEG_QUALITY,
    PAGE_QUIET_PERIOD_S,
    PAGE_SETTLE_TIMEOUT_S,
    SETTLE_POLL_INTERVAL_S,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)
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

PAGE_INSPECTOR_JS = (Path(__file__).parent / "page_inspector.js").read_text(encoding="utf-8")

class BrowserScreen:
    """The Screen interface implemented with Playwright and Chromium."""

    snapshot_name = "dom_snapshot.html"

    def __init__(self, policy: SafetyPolicy, log: RunLog):
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
        self.debug_port: int | None = None
        # Native dialogs (alert/confirm/prompt) never appear in screenshots or the DOM, so
        # each one is answered here, by rule, and recorded for the engine to judge.
        self.dialog_rules: dict = {}  # code -> DialogRule, set from the app profile
        self.dialogs: list[dict] = []

    # ------------------------------------------------------------ lifecycle

    async def start(self, headed: bool = False, debug_port: int | None = None,
                    slow_mo: int = 0, inspector: bool = True,
                    window: tuple[int, int] = (WINDOW_WIDTH, WINDOW_HEIGHT),
                    scale: float = 1.0) -> None:
        """`debug_port` exposes the browser over CDP so an operator tool can attach to the
        same live session (localhost only). `inspector=False` starts a plain window with no
        DOM helper, for the pixel surface; `scale` is its device pixel ratio."""
        self._pw = await async_playwright().start()
        self.debug_port = debug_port
        args = [f"--remote-debugging-port={debug_port}", "--remote-debugging-address=127.0.0.1"] \
            if debug_port else []
        self._browser = await self._pw.chromium.launch(headless=not headed, args=args, slow_mo=slow_mo)
        self.context = await self._browser.new_context(
            viewport={"width": window[0], "height": window[1]}, device_scale_factor=scale)
        if inspector:
            await self.context.add_init_script(script=PAGE_INSPECTOR_JS)
        await self.context.route("**/*", self._filter)
        self.page = await self.context.new_page()
        self.page.on("dialog", self._on_dialog)
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

    async def cookie(self, name: str) -> str | None:
        for c in await self.context.cookies():
            if c["name"] == name:
                return c["value"]
        return None

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

    async def _on_dialog(self, dialog) -> None:
        text = dialog.message
        code = next((c for c, r in self.dialog_rules.items() if r.when.lower() in text.lower()), None)
        if code:
            action = self.dialog_rules[code].action
        else:  # unexpected: acknowledge an alert; cancel anything that asks a question
            action = "accept" if dialog.type == "alert" else "dismiss"
        try:
            await (dialog.accept() if action == "accept" else dialog.dismiss())
        except PlaywrightError:
            pass
        self.dialogs.append({"type": dialog.type, "message": text, "action": action, "code": code})
        label = code or "unexpected"
        self.log.event("dialog", f"{dialog.type} dialog ({label}): {text!r} -> {action}",
                       dialog_type=dialog.type, code=code, action=action)

    async def go_back(self, frame_name: str | None) -> None:
        frame = self.frame(frame_name) or self.page.main_frame
        try:
            await frame.evaluate("history.back()")
        except PlaywrightError:
            await self.page.go_back()
        await self.settle()

    async def reload(self, frame_name: str | None) -> None:
        frame = self.frame(frame_name) or self.page.main_frame
        try:
            await frame.evaluate("location.reload()")
        except PlaywrightError:
            await self.page.reload()
        await self.settle()

    def _on_request(self, _request) -> None:
        self._inflight += 1

    def _on_request_done(self, _request) -> None:
        self._inflight = max(0, self._inflight - 1)

    async def settle(self, timeout: float = PAGE_SETTLE_TIMEOUT_S) -> None:
        """Wait until no request has been in flight and every frame has been loaded for
        PAGE_QUIET_PERIOD_S. A condition, not a fixed pause: a fast page settles quickly."""
        deadline = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < deadline:
            busy = self._inflight > 0 or not await self._all_frames_complete()
            if busy:
                quiet_since = None
            else:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= PAGE_QUIET_PERIOD_S:
                    return
            await asyncio.sleep(SETTLE_POLL_INTERVAL_S)

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
            if not await frame.evaluate("() => !!window.__uiAutomation"):
                await frame.add_script_tag(content=PAGE_INSPECTOR_JS)
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
                data = await frame.evaluate("(start) => window.__uiAutomation.observe(start)", ref)
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

    async def frame_texts(self) -> dict[str, dict]:
        """Visible text and title of every frame, for detectors and checkpoints."""
        out = {}
        for frame in self.frames():
            if not await self._ready(frame):
                continue
            try:
                out[frame.name or "top"] = {
                    "text": await frame.evaluate("() => window.__uiAutomation.text()"),
                    "title": await frame.evaluate("() => window.__uiAutomation.title()"),
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
                data = await frame.evaluate("([s, t]) => window.__uiAutomation.resolve(s, t)", [spec, token])
            except PlaywrightError:
                attempts.append((strategy.kind, 0))
                continue
            attempts.append((strategy.kind, data["count"]))
            if data["count"] == 1:
                return Resolution(frame, frame.locator(f'[data-ua-hit="{token}"]'), i,
                                  data["element"], attempts, frame_url=frame.url)
        return Resolution(None, None, -1, None, attempts)

    async def check_strategy(self, frame_name: str | None, strategy: dict, ref: int) -> bool:
        """True if the strategy matches exactly one element and it is element [ref].
        Used at record time to keep only locators proven on the live screen."""
        frame = self.frame(frame_name)
        if frame is None:
            return False
        try:
            data = await frame.evaluate("([s, t]) => window.__uiAutomation.resolve(s, t)",
                                        [strategy, secrets.token_hex(4)])
        except PlaywrightError:
            return False
        return data["count"] == 1 and data["refs"][0] == str(ref)

    # ------------------------------------------------------------ act

    async def act_ref(self, obs: Observation, ref: int, action: str, value: str | None = None,
                      allow_irreversible: bool = False) -> ActResult:
        """Act on element [ref] of an observation (discovery: the LLM names an element number)."""
        el = obs.elements.get(ref)
        if el is None:
            raise LookupError(f"there is no element [{ref}] on the current screen")
        frame = self.frame(el["frame"])
        if frame is None:
            raise LookupError(f"frame {el['frame']!r} is gone")
        try:
            return await self._act(frame, frame.locator(f'[data-ua-ref="{ref}"]'), el, action, value,
                                   allow_irreversible)
        except PlaywrightError as e:
            raise ScreenError(str(e).splitlines()[0]) from e

    async def act(self, res: Resolution, action: str, value: str | None = None,
                  allow_irreversible: bool = False) -> ActResult:
        """Act on an element found by resolve() (replay)."""
        try:
            return await self._act(res.frame, res.locator, res.element, action, value, allow_irreversible)
        except PlaywrightError as e:
            raise ScreenError(str(e).splitlines()[0]) from e

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
            await locator.click(timeout=ACTION_TIMEOUT_MS)
        elif action == "fill":
            await locator.fill(value or "", timeout=ACTION_TIMEOUT_MS)
        elif action == "select":
            await self._select(locator, value or "")
        elif action == "extract":
            if el.get("tag") in ("input", "textarea", "select"):
                text = await locator.input_value(timeout=ACTION_TIMEOUT_MS)
            else:
                text = (await locator.inner_text(timeout=ACTION_TIMEOUT_MS)).strip()
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
        await locator.select_option(label=picks[0], timeout=ACTION_TIMEOUT_MS)

    # ------------------------------------------------------------ evidence

    async def screenshot(self, path: Path, sensitive: list[str]) -> Path | None:
        """Full-page screenshot with every element showing a sensitive value blacked out."""
        masks = []
        for frame in self.frames():
            if not await self._ready(frame):
                continue
            try:
                if await frame.evaluate("(v) => window.__uiAutomation.markSensitive(v)", sensitive):
                    masks.append(frame.locator("[data-ua-mask]"))
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

    # ------------------------------------------------------------ the person's input (handoff)

    async def install_operator_recorder(self, on_event, script: str) -> None:
        """Record what a person does on this session (see human_handoff/operator_recorder.js)."""
        await self.context.expose_binding("__uiAutomationOperator", on_event)
        await self.context.add_init_script(script=script)

    async def click_at(self, x: float, y: float) -> None:
        await self.page.mouse.click(x, y)

    async def type_text(self, text: str) -> None:
        await self.page.keyboard.type(text)

    async def press_key(self, key: str) -> None:
        await self.page.keyboard.press(key)

    async def capture_frame(self) -> bytes | None:
        """A JPEG of the current screen for the control room's live view (not persisted)."""
        try:
            return await self.page.screenshot(type="jpeg", quality=LIVE_VIEW_JPEG_QUALITY)
        except PlaywrightError:
            return None

    async def bring_to_front(self) -> None:
        try:
            await self.page.bring_to_front()
        except PlaywrightError:
            pass
