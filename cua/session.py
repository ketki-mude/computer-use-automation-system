"""Who controls the live session, and the operator console that hands it to a person.

Control model
-------------
One browser session, one owner at a time. The lease is `owner` plus an `epoch` number:

  automation --(escalate)--> nobody (paused) --(take control)--> human:<operator>
       ^                                                              |
       +---------------(resume / complete: epoch += 1)----------------+

The surface adapter calls `check_lease` before every action, so automation physically
cannot act while a person holds the session. Handing control back increments the epoch.

The person works in the same headed browser window the automation was driving (same
cookies, same page). A listener injected into every frame records their clicks and
changes while they hold the lease. The console here is deliberately minimal; a
production version would stream a remote browser to the operator instead.
"""

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import typer
import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .config import OPERATOR_PORT
from .evidence import RunLog
from .handoff import HandoffResolution
from .schema import Handoff as HandoffRecord
from .surface.web import NotInControl, WebSurface

HUMAN_JS = (Path(__file__).parent / "surface" / "human.js").read_text()
State = Literal["awaiting_human", "human_control", "resumed", "completed_by_human", "aborted",
                "timed_out"]


@dataclass
class Intervention:
    id: str
    run_id: str
    capability: str
    step_id: str
    reason: str
    screenshot: Path | None
    created_at: datetime
    state: State = "awaiting_human"
    operator: str | None = None
    human_actions: list[dict] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class SessionController:
    def __init__(self, surface: WebSurface, log: RunLog, timeout_s: float = 900):
        self.surface = surface
        self.log = log
        self.timeout_s = timeout_s
        self.owner = "automation"
        self.epoch = 1
        self.interventions: dict[str, Intervention] = {}

    async def attach(self) -> None:
        """Call after the browser starts and before the first navigation."""
        self.surface.lease_check = self.check_lease
        await self.surface.context.expose_binding("__cuaHuman", self._on_human_event)
        await self.surface.context.add_init_script(script=HUMAN_JS)

    def check_lease(self) -> None:
        if self.owner != "automation":
            raise NotInControl(f"the session is held by {self.owner or 'nobody (paused)'}")

    # ------------------------------------------------------------ the Handoff interface

    async def request(self, *, run_id: str, capability: str, step_id: str, reason: str,
                      screenshot: Path | None) -> HandoffResolution:
        iv = Intervention(id=f"iv-{secrets.token_hex(3)}", run_id=run_id, capability=capability,
                          step_id=step_id, reason=reason, screenshot=screenshot,
                          created_at=datetime.now(UTC))
        self.interventions[iv.id] = iv
        self.owner = ""  # paused: nobody may act until an operator takes control
        self.log.event("intervention_raised", f"{iv.id} at {step_id}: {reason}",
                       intervention=iv.id, step_id=step_id, reason=reason)
        typer.secho(f"\n  >> Human needed. Open http://127.0.0.1:{OPERATOR_PORT} and take control "
                    f"of intervention {iv.id}.\n", fg=typer.colors.MAGENTA, bold=True)
        try:
            await asyncio.wait_for(iv.done.wait(), timeout=self.timeout_s)
        except TimeoutError:
            iv.state = "timed_out"
            self.log.event("intervention_timed_out", f"{iv.id}: no operator within {self.timeout_s:.0f}s")
        self.owner = "automation"
        resolution = {"resumed": "resumed", "completed_by_human": "completed_by_human",
                      "aborted": "aborted"}.get(iv.state, "timed_out")
        record = HandoffRecord(intervention_id=iv.id, reason=reason, operator=iv.operator,
                               resolution=resolution, human_actions=len(iv.human_actions))
        self.log.write_json(f"interventions/{iv.id}.json", {
            "id": iv.id, "step_id": step_id, "reason": reason, "state": iv.state,
            "operator": iv.operator, "human_actions": iv.human_actions, "epoch": self.epoch})
        return HandoffResolution(record=record, human_actions=list(iv.human_actions))

    # ------------------------------------------------------------ operator actions

    def _open(self, iv_id: str, *states: State) -> Intervention:
        iv = self.interventions.get(iv_id)
        if iv is None or iv.state not in states:
            raise ValueError(f"intervention {iv_id} is not in state {' or '.join(states)}")
        return iv

    async def take_control(self, iv_id: str, operator: str) -> None:
        iv = self._open(iv_id, "awaiting_human")
        iv.state, iv.operator = "human_control", operator
        self.owner = f"human:{operator}"
        self.log.event("intervention_taken", f"{operator} took control of the live session ({iv.id})",
                       intervention=iv.id, operator=operator)
        await self.surface.page.bring_to_front()

    def _hand_back(self, iv: Intervention, state: State, message: str) -> None:
        iv.state = state
        self.epoch += 1
        self.owner = "automation"
        self.log.event("intervention_" + state, f"{iv.id}: {message} (epoch {self.epoch})",
                       intervention=iv.id, human_actions=len(iv.human_actions))
        iv.done.set()

    def resume(self, iv_id: str) -> None:
        iv = self._open(iv_id, "human_control")
        self._hand_back(iv, "resumed", f"{iv.operator} handed control back to automation")

    def complete(self, iv_id: str) -> None:
        iv = self._open(iv_id, "human_control")
        self._hand_back(iv, "completed_by_human", f"{iv.operator} finished the task by hand")

    def abort(self, iv_id: str) -> None:
        iv = self._open(iv_id, "awaiting_human", "human_control")
        self._hand_back(iv, "aborted", "the operator aborted the run")

    def _on_human_event(self, _source, event: dict) -> None:
        if not self.owner.startswith("human:"):
            return  # automation's own clicks also fire these listeners
        iv = next((i for i in self.interventions.values() if i.state == "human_control"), None)
        if iv is None:
            return
        target = event.get("target") or {}
        summary = {"action": event.get("type"), "role": target.get("role"), "name": target.get("name"),
                   "label": target.get("label") or target.get("column"), "value": event.get("value")}
        summary = self.log.redactor.scrub(summary)
        iv.human_actions.append(summary)
        shown = f'{summary["action"]} {summary["role"]} "{summary["name"] or summary["label"] or ""}"'
        if summary["value"]:
            shown += f" <- {summary['value']}"
        self.log.event("human_action", shown, intervention=iv.id, **summary)


# ------------------------------------------------------------------ operator console

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="2">
<title>Operator console</title><style>
body{{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1b2230}}
.card{{border:1px solid #c9d1dd;border-radius:10px;padding:14px;margin:14px 0}}
.awaiting_human{{border-color:#6a3db0;background:#f6f1fd}} .human_control{{border-color:#6a3db0;border-width:2px}}
.chip{{font:12px monospace;padding:2px 8px;border-radius:99px;background:#ece3f8;color:#6a3db0}}
img{{max-width:100%;border:1px solid #ccc;margin-top:8px}} button{{font:inherit;padding:6px 12px;margin-right:6px}}
code{{background:#eef1f5;padding:1px 4px;border-radius:4px}} .muted{{color:#5a6478}}</style></head><body>
<h1>Operator console</h1>
<p>Session owner: <code>{owner}</code> &middot; epoch {epoch}</p>
<p class="muted">After <b>Take control</b>, work in the browser window the automation opened (same session).
Your clicks and changes are recorded; typed values are masked. Then hand control back.</p>
{cards}</body></html>"""


def _card(iv: Intervention) -> str:
    actions = "".join(f"<li><code>{a['action']}</code> {a.get('role') or ''} "
                      f"{a.get('name') or a.get('label') or ''} {a.get('value') or ''}</li>"
                      for a in iv.human_actions) or "<li class='muted'>none yet</li>"
    buttons = ""
    if iv.state == "awaiting_human":
        buttons = (f"<form method=post action=/take/{iv.id} style='display:inline'>"
                   f"<input name=operator value=op1 size=6> <button>Take control</button></form>"
                   f"<form method=post action=/abort/{iv.id} style='display:inline'><button>Abort</button></form>")
    elif iv.state == "human_control":
        buttons = "".join(f"<form method=post action=/{a}/{iv.id} style='display:inline'><button>{label}</button></form>"
                          for a, label in (("resume", "Resume automation"), ("complete", "Mark completed"),
                                           ("abort", "Abort")))
    shot = f"<img src='/shot/{iv.id}' alt='screenshot, sensitive fields masked'>" if iv.screenshot else ""
    return (f"<div class='card {iv.state}'><b>{iv.id}</b> <span class=chip>{iv.state}</span>"
            f"<div>Capability <code>{iv.capability}</code> &middot; step <code>{iv.step_id}</code></div>"
            f"<div>Reason: {iv.reason}</div><div>Operator: {iv.operator or '-'}</div>"
            f"<div>Recorded human actions:<ul>{actions}</ul></div>{buttons}{shot}</div>")


def build_console(controller: SessionController) -> FastAPI:
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/", response_class=HTMLResponse)
    def index():
        cards = "".join(_card(iv) for iv in reversed(list(controller.interventions.values())))
        return PAGE.format(owner=controller.owner or "nobody (paused)", epoch=controller.epoch,
                           cards=cards or "<p class=muted>No interventions. Automation is in control.</p>")

    @api.get("/shot/{iv_id}")
    def shot(iv_id: str):
        iv = controller.interventions.get(iv_id)
        if iv is None or iv.screenshot is None:
            return HTMLResponse("not found", status_code=404)
        return FileResponse(iv.screenshot)

    @api.get("/api/interventions")
    def listing():
        return [{"id": iv.id, "state": iv.state, "step_id": iv.step_id, "reason": iv.reason,
                 "operator": iv.operator, "human_actions": iv.human_actions}
                for iv in controller.interventions.values()]

    async def act(fn, *args):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except ValueError as e:
            return HTMLResponse(str(e), status_code=409)
        return RedirectResponse("/", status_code=303)

    @api.post("/take/{iv_id}")
    async def take(iv_id: str, operator: str = Form("op1")):
        return await act(controller.take_control, iv_id, operator)

    @api.post("/resume/{iv_id}")
    async def resume(iv_id: str):
        return await act(controller.resume, iv_id)

    @api.post("/complete/{iv_id}")
    async def complete(iv_id: str):
        return await act(controller.complete, iv_id)

    @api.post("/abort/{iv_id}")
    async def abort(iv_id: str):
        return await act(controller.abort, iv_id)

    return api


class Console:
    def __init__(self, server: uvicorn.Server, task: asyncio.Task):
        self._server, self._task = server, task

    async def stop(self) -> None:
        self._server.should_exit = True
        await self._task


async def start_console(controller: SessionController, port: int = OPERATOR_PORT) -> Console:
    """Serve the console from the same event loop as the run, so it shares the controller."""
    server = uvicorn.Server(uvicorn.Config(build_console(controller), host="127.0.0.1", port=port,
                                           log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # surfaces e.g. "address already in use"
        await asyncio.sleep(0.05)
    typer.secho(f"Operator console: http://127.0.0.1:{port}", fg=typer.colors.MAGENTA)
    return Console(server, task)
