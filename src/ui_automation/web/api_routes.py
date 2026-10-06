"""Pages and JSON endpoints.

Two pages: the Ask page (/ask), where a requester types a request and gets an answer, and
the control room (/control), where staff work tickets, watch runs, review capabilities and
use the demo's test controls. Ticket endpoints are also used by runs started from the
command line, which post their tickets here over HTTP.
"""

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..catalog import capability_review
from ..catalog.agent_tools import tool_catalog
from ..catalog.capability_store import CapabilityStore
from ..human_handoff.control_lease import SessionController
from ..human_handoff.tickets import NewTicket, TicketInbox
from ..screen.screen_interface import NotInControl
from ..settings import (
    BANK_ADMIN_TIMEOUT_S,
    INVOKE_POLL_INTERVAL_S,
    INVOKE_WAIT_S,
    WATCHABLE_SLOW_MO_MS,
)
from .control_room import ControlRoom, catalog

WEB_DIR = Path(__file__).parent
ASK_HTML = (WEB_DIR / "templates" / "ask.html").read_text(encoding="utf-8")
CONTROL_ROOM_HTML = (WEB_DIR / "templates" / "control_room.html").read_text(encoding="utf-8")
# Test controls: what the next run will hit. Keys are what the control room's buttons send.
FAULTS = {
    "maintenance": {"maintenance": 1}, "expire": {"expire_after": 2},
    "identity": {"verify_identity": 1}, "error": {"app_error": 1}, "slow": {"slow_ms": 2500},
    "popup": {"popup": 1}, "unavailable": {"unavailable": 1}, "variant": {"variant": 1},
}
HumanKey = Literal["Enter", "Tab", "Backspace", "Escape"]


class OperatorBody(BaseModel):
    operator: str | None = None


class AskBody(BaseModel):
    query: str = ""
    capability: str | None = None  # run this capability with `params` (the inputs form)
    params: dict[str, str] = {}
    learn: bool = False  # skip reuse and discover a new capability


class ReviewBody(BaseModel):
    by: str = ""
    reason: str = ""


class InvokeBody(BaseModel):
    inputs: dict[str, str] = {}


class SettingsBody(BaseModel):
    watchable: bool | None = None  # slow every browser action down
    show_screen: bool | None = None  # show the bank screen on the requests page (demo)


class StaffBody(BaseModel):
    operator: str = Field(min_length=1)


class ClickBody(BaseModel):
    x: float
    y: float


class TypeBody(BaseModel):
    text: str


class KeyBody(BaseModel):
    key: HumanKey


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def register_ticket_routes(app: FastAPI, inbox: TicketInbox) -> None:
    """JSON endpoints used by runs (create, poll, report actions) and by the control room."""

    @app.post("/api/tickets")
    def create_ticket(new: NewTicket):
        return {"id": inbox.create(new).id}

    @app.get("/api/tickets")
    def list_tickets():
        return [inbox.view(t) for t in inbox.tickets.values()]

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: str):
        t = inbox.tickets.get(ticket_id)
        return inbox.view(t) if t else error(f"no ticket {ticket_id}", 404)

    @app.get("/api/tickets/{ticket_id}/shot")
    def ticket_screenshot(ticket_id: str):
        t = inbox.tickets.get(ticket_id)
        if t is None or not t.screenshot_png_b64:
            return Response(status_code=404)
        return Response(base64.b64decode(t.screenshot_png_b64), media_type="image/png")

    @app.post("/api/tickets/{ticket_id}/actions")
    def add_human_actions(ticket_id: str, actions: list[dict]):
        t = inbox.tickets.get(ticket_id)
        if t is None:
            return error(f"no ticket {ticket_id}", 404)
        t.human_actions.extend(actions)
        return {"count": len(t.human_actions)}

    @app.post("/api/tickets/{ticket_id}/{action}")
    def act_on_ticket(ticket_id: str,
                      action: Literal["take", "resume", "done", "reject", "abort", "timed_out"],
                      body: OperatorBody | None = None):
        try:
            t = inbox.act(ticket_id, action, operator=body.operator if body else None)
        except KeyError as e:
            return error(str(e).strip("'"), 404)
        except ValueError as e:
            return error(str(e), 409)
        return inbox.view(t)


def _capability_summaries() -> list[dict]:
    return [{"name": c.name, "version": c.version, "ref": c.ref, "status": c.status,
             "risk": c.risk, "description": c.description} for c in catalog()]


def create_app(room: ControlRoom) -> FastAPI:
    """Both pages and their APIs, sharing one ControlRoom."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    register_ticket_routes(app, room.inbox)

    # ---------------------------------------------------------------- pages

    @app.get("/")
    def home():
        return RedirectResponse("/ask")

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page():
        return ASK_HTML

    @app.get("/control", response_class=HTMLResponse)
    def control_room_page():
        return CONTROL_ROOM_HTML

    # ---------------------------------------------------------------- requests (Ask page)

    @app.post("/api/ask")
    async def ask(body: AskBody):
        query = body.query.strip()
        if not query and not body.capability:
            return error("type a request first", 400)
        job = await room.ask(query or f"run {body.capability}", body.capability, body.params,
                             body.learn)
        return {"id": job.id}

    @app.get("/api/requests")
    def requests():
        jobs = sorted(room.jobs.values(), key=lambda j: j.created, reverse=True)
        return {"jobs": [j.summary() for j in jobs], "ai": room.ai,
                "show_screen": room.show_screen_to_requester}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        j = room.jobs.get(job_id)
        return j.detail() if j else error("no such request", 404)

    @app.get("/api/jobs/{job_id}/screen")
    def live_screen(job_id: str):
        j = room.jobs.get(job_id)
        if j is None or j.frame is None:
            return Response(status_code=404)
        return Response(j.frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- agent-facing tools

    @app.get("/api/tools")
    def tools():
        """Approved capabilities as function-calling tools, for an AI agent to choose from."""
        return {"tools": tool_catalog(catalog()),
                "invoke": "POST /api/capabilities/{name}/invoke with {\"inputs\": {...}}",
                "result_schema": "schemas/run_result.schema.json"}

    @app.post("/api/capabilities/{name}/invoke")
    async def invoke(name: str, body: InvokeBody):
        """Run an approved capability by name with typed inputs; no LLM involved. Returns the
        result contract, or 202 with the job to poll if the run is waiting for a person."""
        cap = next((c for c in catalog() if c.name == name and c.status == "approved"), None)
        if cap is None:
            return error(f"no approved capability {name}", 404)
        job = await room.ask(f"{name}({', '.join(body.inputs)})", name, body.inputs,
                             how="invoked as a tool by an agent")
        deadline = time.monotonic() + INVOKE_WAIT_S
        while job.result is None and time.monotonic() < deadline:
            await asyncio.sleep(INVOKE_POLL_INTERVAL_S)
        if job.result is None:
            return JSONResponse({"job": job.id, "status": "waiting", "ticket": job.ticket_id,
                                 "poll": f"/api/jobs/{job.id}"}, status_code=202)
        return job.result

    # ---------------------------------------------------------------- control room

    @app.get("/api/control/state")
    def control_state():
        jobs = sorted(room.jobs.values(), key=lambda j: j.created, reverse=True)
        return {"jobs": [j.summary() for j in jobs],
                "tickets": [{**room.inbox.view(t), "job": room.ticket_jobs.get(t.id)}
                            for t in room.inbox.tickets.values()],
                "capabilities": _capability_summaries(), "ai": room.ai, "bank": room.bank_url,
                "watchable": room.slow_mo > 0, "show_screen": room.show_screen_to_requester}

    @app.post("/api/settings")
    def update_settings(body: SettingsBody):
        if body.watchable is not None:
            room.slow_mo = WATCHABLE_SLOW_MO_MS if body.watchable else 0
        if body.show_screen is not None:
            room.show_screen_to_requester = body.show_screen
        return {"watchable": room.slow_mo > 0, "show_screen": room.show_screen_to_requester}

    async def as_human(job_id: str, act: Callable[[SessionController], Awaitable[None]]):
        """Forward one click / keystroke from the person holding this run's ticket."""
        j = room.jobs.get(job_id)
        if j is None or j.controller is None or j.screen is None:
            return error("this run cannot be controlled", 404)
        try:
            await act(j.controller)
        except NotInControl as e:
            return error(str(e), 409)
        await j.screen.settle()
        j.frame = await j.screen.capture_frame() or j.frame
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/takeover")
    def take_over(job_id: str, body: StaffBody):
        """Staff steps in: the run pauses after its current step and raises a takeover ticket."""
        j = room.jobs.get(job_id)
        if j is None or not j.live or j.controller is None:
            return error("this run is not running, or cannot be handed to a person", 409)
        try:
            j.controller.ask_for("takeover", body.operator.strip())
        except ValueError as e:
            return error(str(e), 409)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/stop")
    def stop_run(job_id: str, body: StaffBody):
        """Staff stops a run: after its current step, or at once if it is waiting on a ticket."""
        j = room.jobs.get(job_id)
        if j is None or not j.live or j.controller is None:
            return error("this run is not running", 409)
        if j.ticket_id:
            room.inbox.act(j.ticket_id, "abort", operator=body.operator.strip())
        else:
            j.controller.ask_for("stop", body.operator.strip())
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/click")
    async def human_click(job_id: str, body: ClickBody):
        return await as_human(job_id, lambda c: c.human_click(body.x, body.y))

    @app.post("/api/jobs/{job_id}/type")
    async def human_type(job_id: str, body: TypeBody):
        return await as_human(job_id, lambda c: c.human_type(body.text))

    @app.post("/api/jobs/{job_id}/key")
    async def human_key(job_id: str, body: KeyBody):
        return await as_human(job_id, lambda c: c.human_press(body.key))

    # ---------------------------------------------------------------- capability review

    @app.get("/api/capabilities/{name}/review")
    def review(name: str, version: str | None = None):
        try:
            cap = CapabilityStore().load(name, version)
        except LookupError as e:
            return error(str(e), 404)
        return capability_review.review_card(cap)

    @app.post("/api/capabilities/{name}/{decision}")
    def decide(name: str, decision: Literal["approve", "reject"], body: ReviewBody,
               version: str | None = None):
        store = CapabilityStore()
        by = body.by.strip() or "reviewer"
        try:
            if decision == "approve":
                cap = capability_review.approve(store, name, by, version)
            else:
                cap = capability_review.reject(store, name, by, body.reason, version)
        except LookupError as e:
            return error(str(e), 404)
        except capability_review.ReviewRefused as e:
            return error(str(e), 409)
        return {"ref": cap.ref, "status": cap.status}

    @app.get("/capabilities/{name}", response_class=PlainTextResponse)
    def capability_yaml(name: str, version: str | None = None):
        store = CapabilityStore()
        try:
            return store.path(store.load(name, version)).read_text(encoding="utf-8")
        except LookupError as e:
            return PlainTextResponse(str(e), status_code=404)

    # ---------------------------------------------------------------- test controls

    @app.post("/api/bank/{action}")
    async def bank_fault(action: str):
        if action != "reset" and action not in FAULTS:
            return error(f"unknown test control {action}", 400)
        path, body = ("reset", {}) if action == "reset" else ("faults", FAULTS[action])
        try:
            async with httpx.AsyncClient(timeout=BANK_ADMIN_TIMEOUT_S) as client:
                response = await client.post(f"{room.bank_url}/__admin/{path}", json=body)
                response.raise_for_status()
        except httpx.HTTPError as e:
            return error(f"the mock bank did not accept it: {e}", 502)
        return {"ok": True}

    return app
