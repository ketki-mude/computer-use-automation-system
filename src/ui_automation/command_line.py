"""The `ui-automation` command line. Commands only: each one calls core functions.

  ui-automation demo [--slow]           the Ask page, the control room and the mock bank
  ui-automation discover "goal"         the AI accomplishes the goal; the run is compiled into a
                                        capability, validated by a replay, and saved as a draft
  ui-automation replay NAME -p k=v      run a capability with no AI; prints the result contract
                                        (--surface pixel: screenshots and mouse only, no DOM)
  ui-automation capabilities            list saved capabilities
  ui-automation show NAME               print a capability
  ui-automation approve NAME            approve a validated draft for unattended replay
  ui-automation reject NAME -r why      reject a draft (kept for the record, never replayed)
  ui-automation fault KEY=VALUE         make the mock bank misbehave on the next run
  ui-automation reset-bank              restore the mock bank's data, clear faults and sessions
  ui-automation schemas                 write the JSON Schemas other teams integrate against

Exit codes for discover and replay: 0 success (or business outcome), 1 failed, 3 escalated.
"""

import asyncio
import sys
from pathlib import Path

import httpx
import typer

from mock_bank.bank_app import seed_demo_credentials

from . import capability_workflows
from .catalog import capability_review
from .catalog.agent_tools import write_schemas
from .catalog.capability_store import CapabilityStore
from .config_files import ConfigError, load_app_profile
from .discovery.llm_clients import ScriptedClient, configured_client
from .human_handoff.tickets import HttpTicketInbox
from .models.app_profile import AppProfile
from .models.run_result import RunResult
from .settings import (
    BANK_ADMIN_TIMEOUT_S,
    BANK_PORT,
    DEFAULT_APP,
    DISCOVERY_MAX_STEPS,
    OPERATOR_PORT,
    PIXEL_SCALE,
    ROOT,
    WATCHABLE_SLOW_MO_MS,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)
EXIT = {"success": 0, "business_outcome": 0, "failed": 1, "escalated": 3}


@app.callback()
def main() -> None:
    """Discover capabilities with an LLM, then replay them without one."""
    for stream in (sys.stdout, sys.stderr):
        # A Windows pipe or file uses the locale code page; a mask character it lacks ("•")
        # must print as "?" rather than crash the command.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    seed_demo_credentials()  # the mock bank's synthetic sign-on, unless set in the environment


def load_profile(app_id: str) -> AppProfile:
    try:
        return load_app_profile(app_id)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from e


def operator_inbox(operator: bool) -> HttpTicketInbox | None:
    if not operator:
        return None
    inbox = HttpTicketInbox()
    if not asyncio.run(inbox.ping()):
        typer.secho(f"--operator needs the control room running: start it with `ui-automation demo` "
                    f"(expected at http://127.0.0.1:{OPERATOR_PORT}).", fg=typer.colors.RED)
        raise typer.Exit(1)
    return inbox


# ------------------------------------------------------------------ demo

@app.command()
def demo(slow: bool = typer.Option(False, help="Slow every browser action down so you can watch"),
         open_browser: bool = typer.Option(True, "--open/--no-open", help="Open both pages")):
    """Start the Ask page, the control room and the mock bank. Everything else is in the browser.

    Ports come from UI_AUTOMATION_OPERATOR_PORT (default 8001) and UI_AUTOMATION_BANK_PORT
    (default 8000), so config files and runs always agree on them."""
    from .web.web_app import run_demo
    try:
        asyncio.run(run_demo(WATCHABLE_SLOW_MO_MS if slow else 0, open_browser))
    except KeyboardInterrupt:
        typer.echo("Stopped.")
    except OSError as e:
        typer.secho(f"Could not start: {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e


# ------------------------------------------------------------------ discover

@app.command()
def discover(
    goal: str = typer.Argument(..., help='e.g. "look up member 12345 and read their savings balance"'),
    app_id: str = typer.Option(DEFAULT_APP, "--app", help="App profile in config/apps/"),
    max_steps: int = typer.Option(DISCOVERY_MAX_STEPS, help="Escalate after this many actions"),
    headed: bool = typer.Option(False, help="Show the browser window"),
    operator: bool = typer.Option(False, help="Send tickets to the control room (ui-automation demo)"),
    save: bool = typer.Option(True, "--save/--no-save", help="Save the validated capability"),
    allow_irreversible: bool = typer.Option(
        False, help="Let discovery commit irreversible steps itself (test environments only)"),
    script: Path | None = typer.Option(
        None, help="Offline: take decisions from a YAML script instead of Gemini (no API key)"),
    slow: bool = typer.Option(False, help="Slow every action down so you can watch"),
):
    """Let the LLM accomplish GOAL on the live app, then compile and validate a capability."""
    profile = load_profile(app_id)
    inbox = operator_inbox(operator)
    log = capability_workflows.new_run_log("discover", profile)
    typer.secho(f"Discovery run {log.run_id}  (evidence: {log.dir})", bold=True)
    llm = ScriptedClient(script) if script else configured_client(log)
    if llm is None:
        log.close()
        typer.secho("No GEMINI_API_KEY is set (see .env.example). Run offline with "
                    "--script scripted_discovery/<name>.yaml instead.", fg=typer.colors.RED)
        raise typer.Exit(1)
    outcome = asyncio.run(capability_workflows.learn_capability(
        goal, profile, log, llm, headed=headed or operator, inbox=inbox, max_steps=max_steps,
        allow_irreversible=allow_irreversible, save=save,
        slow_mo=WATCHABLE_SLOW_MO_MS if slow else 0))
    result = outcome.result
    color = typer.colors.GREEN if outcome.ok else typer.colors.YELLOW
    typer.secho(f"\nDiscovery {result.status}: {result.reason}", fg=color, bold=True)
    if "429" in result.reason or "RESOURCE_EXHAUSTED" in result.reason:
        typer.secho("Gemini's free daily quota is used up. Try again after it resets, use a key "
                    "with billing, or run offline with --script scripted_discovery/<name>.yaml.",
                    fg=typer.colors.YELLOW)
    if outcome.validation is not None and not outcome.ok:
        _print_result(outcome.validation)
    typer.secho(outcome.message, fg=color, bold=True)
    if outcome.saved_to:
        typer.echo(f"Review it ({outcome.saved_to}), then: ui-automation approve {outcome.capability.name}")
    if outcome.ok:
        raise typer.Exit(0)
    raise typer.Exit(3 if result.status == "escalated" else 1)


# ------------------------------------------------------------------ replay

def _parse_params(pairs: list[str]) -> dict[str, str]:
    params = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"expected name=value, got {pair!r}")
        k, v = pair.split("=", 1)
        params[k.strip()] = v
    return params


@app.command()
def replay(
    name: str = typer.Argument(..., help="Capability name"),
    param: list[str] | None = typer.Option(None, "--param", "-p", help="Input as name=value (repeatable)"),
    version: str | None = typer.Option(None, help="Version to run (default: latest)"),
    allow_draft: bool = typer.Option(False, help="Run a capability that is not approved yet"),
    allow_irreversible: bool = typer.Option(
        False, help="Let automation perform irreversible steps itself (otherwise a person must)"),
    headed: bool = typer.Option(False, help="Show the browser window"),
    operator: bool = typer.Option(False, help="Send tickets to the control room (ui-automation demo)"),
    slow: bool = typer.Option(False, help="Slow every action down so you can watch"),
    keep_open: bool = typer.Option(False, help="Keep the browser open at the end until Enter"),
    surface: str = typer.Option("web", help="web (the DOM) or pixel (screenshots and mouse only)"),
    window: str = typer.Option(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}",
                               help="Pixel surface: the app window's size in points, WxH"),
    scale: float = typer.Option(PIXEL_SCALE, help="Pixel surface: display scale (2 = 200%)"),
    as_json: bool = typer.Option(False, "--json", help="Print only the result JSON"),
):
    """Replay a capability deterministically, with no LLM."""
    try:
        cap = CapabilityStore().load(name, version)
    except LookupError as e:
        raise typer.BadParameter(str(e)) from e
    if cap.status != "approved" and not allow_draft:
        typer.secho(f"{cap.ref} is a {cap.status}. Approve it (ui-automation approve {cap.name}) or pass "
                    "--allow-draft.", fg=typer.colors.RED)
        raise typer.Exit(1)
    profile = load_profile(cap.app.id)
    inbox = operator_inbox(operator)
    log = capability_workflows.new_run_log("replay", profile, quiet=as_json)
    if not as_json:
        typer.secho(f"Replay run {log.run_id}  (evidence: {log.dir})", bold=True)
    pixel = _pixel_surface(surface, window, scale)
    result = asyncio.run(capability_workflows.run_capability(
        cap, _parse_params(param or []), profile, log, pixel=pixel,
        headed=headed or operator or keep_open,
        inbox=inbox, allow_irreversible=allow_irreversible,
        slow_mo=WATCHABLE_SLOW_MO_MS if slow else 0,
        keep_open=keep_open))
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_result(result)
    raise typer.Exit(EXIT[result.status])


def _pixel_surface(surface: str, window: str, scale: float):
    if surface == "web":
        return None
    if surface != "pixel":
        raise typer.BadParameter("surface is web or pixel")
    from .screen.pixel_screen import PixelSurface

    try:
        width, height = (int(n) for n in window.lower().split("x"))
    except ValueError as e:
        raise typer.BadParameter(f"window is WIDTHxHEIGHT, got {window!r}") from e
    return PixelSurface((width, height), scale)


def _print_result(r: RunResult) -> None:
    color = {"success": typer.colors.GREEN, "business_outcome": typer.colors.CYAN,
             "failed": typer.colors.RED, "escalated": typer.colors.MAGENTA}[r.status]
    typer.secho(f"\nResult: {r.status.upper()}", fg=color, bold=True)
    typer.echo(r.model_dump_json(indent=2, exclude_defaults=True))


# ------------------------------------------------------------------ mock bank test harness

def _bank_admin(path: str, body: dict) -> dict:
    url = f"http://127.0.0.1:{BANK_PORT}/__admin/{path}"
    try:
        response = httpx.post(url, json=body, timeout=BANK_ADMIN_TIMEOUT_S)
        response.raise_for_status()
    except httpx.HTTPError as e:
        typer.secho(f"The mock bank did not accept it ({e}). Is `ui-automation demo` running?",
                    fg=typer.colors.RED)
        raise typer.Exit(1) from e
    return response.json()


def _split_pair(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise typer.BadParameter(f"expected KEY=VALUE, got {pair!r}")
    key, value = pair.split("=", 1)
    return key.strip(), value.strip()


def _fault_value(raw: str) -> bool | int:
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError as e:
        raise typer.BadParameter(f"a fault value is a number or true/false, got {raw!r}") from e


@app.command()
def fault(settings: list[str] = typer.Argument(..., metavar="KEY=VALUE",
                                               help="e.g. maintenance=1 expire_after=2 verify_identity=1")):
    """Make the mock bank misbehave on the next run (maintenance notice, session expiry, ...)."""
    pairs = dict(_split_pair(s) for s in settings)
    typer.echo(_bank_admin("faults", {k: _fault_value(v) for k, v in pairs.items()}))


@app.command("reset-bank")
def reset_bank():
    """Restore the mock bank's sample data and clear faults and sessions."""
    typer.echo(_bank_admin("reset", {}))


# ------------------------------------------------------------------ catalog

@app.command()
def schemas() -> None:
    """Write the JSON Schemas (capability, result contract, app profile) to schemas/."""
    for path in write_schemas():
        typer.echo(f"wrote {path.relative_to(ROOT)}")


@app.command()
def capabilities() -> None:
    """List saved capabilities."""
    caps = CapabilityStore().all()
    if not caps:
        typer.echo("No capabilities yet. Run `ui-automation discover` first.")
        return
    for c in caps:
        inputs = ", ".join(f"{k}: {v.type}" for k, v in c.inputs.items())
        outputs = ", ".join(f"{k}: {v.type}" for k, v in c.outputs.items())
        typer.echo(f"{c.ref:<34} {c.status:<9} risk={c.risk:<12} ({inputs}) -> ({outputs})")
        typer.secho(f"    {c.description}", dim=True)


@app.command()
def show(name: str, version: str | None = None) -> None:
    """Print a capability's YAML."""
    store = CapabilityStore()
    try:
        cap = store.load(name, version)
    except LookupError as e:
        raise typer.BadParameter(str(e)) from e
    typer.echo(store.path(cap).read_text(encoding="utf-8"))


def _review(decide) -> None:
    try:
        cap = decide(CapabilityStore())
    except (LookupError, capability_review.ReviewRefused) as e:
        typer.secho(f"Refused: {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e
    who = cap.provenance.approved_by or cap.provenance.rejected_by
    typer.secho(f"{cap.ref} {cap.status} by {who}", fg=typer.colors.GREEN)


@app.command()
def approve(name: str, version: str | None = typer.Option(None, help="Default: the newest draft"),
            by: str = typer.Option("reviewer", help="Who approved it")) -> None:
    """Approve a validated draft for unattended replay (the flow itself is not changed)."""
    _review(lambda store: capability_review.approve(store, name, by, version))


@app.command()
def reject(name: str, reason: str = typer.Option(..., "--reason", "-r", help="Why it is rejected"),
           version: str | None = typer.Option(None, help="Default: the newest draft"),
           by: str = typer.Option("reviewer", help="Who rejected it")) -> None:
    """Reject a draft. It stays on disk for the record and is never offered for replay."""
    _review(lambda store: capability_review.reject(store, name, by, reason, version))


if __name__ == "__main__":
    app()
