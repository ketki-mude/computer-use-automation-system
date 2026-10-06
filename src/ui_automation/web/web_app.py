"""Runs the demo: the web app (Ask page + control room) and the mock bank, in one process."""

import asyncio
import webbrowser

import typer
import uvicorn

from ..settings import BANK_PORT, BANK_URL, OPERATOR_PORT
from .api_routes import create_app
from .control_room import ControlRoom

SERVER_START_POLL_S = 0.05
LOCALHOST = "127.0.0.1"


async def run_demo(slow_mo: int = 0, open_browser: bool = False) -> None:
    """Serve until Ctrl+C. Raises OSError at start-up if a port is already taken."""
    from mock_bank.bank_app import app as bank_app

    port, bank_port = OPERATOR_PORT, BANK_PORT
    room = ControlRoom(BANK_URL, slow_mo=slow_mo)
    servers = [uvicorn.Server(uvicorn.Config(app, host=LOCALHOST, port=p, log_level="warning"))
               for app, p in ((create_app(room), port), (bank_app, bank_port))]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        for task in tasks:
            if task.done():
                task.result()  # surfaces "address already in use"
                raise OSError(f"a server stopped during start-up (is port {port} or "
                              f"{bank_port} already in use?)")
        await asyncio.sleep(SERVER_START_POLL_S)
    web = f"http://{LOCALHOST}:{port}"
    typer.echo(f"Ask page:      {web}/ask       (type a request, get an answer)")
    typer.echo(f"Control room:  {web}/control   (tickets, runs, capability review, test controls)")
    typer.echo(f"Mock bank:     {BANK_URL}   (supervisor's device for one-time codes: /__admin)")
    typer.echo(f"Speed: {'watchable (slowed down)' if slow_mo else 'fast (real timings)'}. "
               "Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(f"{web}/control")
        webbrowser.open(f"{web}/ask")
    await asyncio.gather(*tasks)
