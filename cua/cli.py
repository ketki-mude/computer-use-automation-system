"""The `cua` command line.

  cua discover "goal"            LLM accomplishes the goal; the run is compiled into a
                                 capability, validated by a replay, and saved as a draft
  cua replay NAME -p k=v         run a capability with no LLM; prints the result contract
  cua capabilities               list saved capabilities
  cua show NAME                  print a capability
  cua approve NAME               mark a capability approved for unattended replay

Exit codes for replay: 0 success or business outcome, 1 failed, 3 escalated.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from .agent import DiscoveryAgent, DiscoveryResult
from .compiler import CompileError, _slug, compile_capability
from .config import CDP_PORT, CONFIG_DIR, DISCOVERY_MODEL, FALLBACK_MODELS, GEMINI_API_KEY
from .evidence import RunLog
from .executor import Executor
from .llm.gemini import GeminiClient
from .llm.scripted import ScriptedClient
from .policy import Policy
from .redact import Redactor
from .registry import Registry
from .replay import ReplayEngine, parse_output
from .schema import AppProfile, Capability, RunResult
from .surface.web import WebSurface

app = typer.Typer(no_args_is_help=True, add_completion=False)
EXIT = {"success": 0, "business_outcome": 0, "failed": 1, "escalated": 3}


@app.callback()
def main() -> None:
    """Discover capabilities with an LLM, then replay them without one."""


def load_profile(app_id: str) -> AppProfile:
    path = CONFIG_DIR / "apps" / f"{app_id}.yaml"
    if not path.exists():
        raise typer.BadParameter(f"no app profile at {path}")
    return AppProfile.model_validate(yaml.safe_load(path.read_text()))


@asynccontextmanager
async def browser_session(log: RunLog, headed: bool, operator: bool):
    """A browser for one run. With `operator`, also a session controller, the operator
    console, a headed window and a localhost CDP port, so a person can take over."""
    surface = WebSurface(Policy.load(), log)
    controller = console = None
    if operator:
        from .session import SessionController, start_console
        controller = SessionController(surface, log)
        console = await start_console(controller)
    try:
        await surface.start(headed=headed or operator, debug_port=CDP_PORT if operator else None)
        if controller is not None:
            await controller.attach()
        yield surface, controller
    finally:
        await surface.close()
        if console is not None:
            await console.stop()


def trace_json(result: DiscoveryResult) -> dict:
    return {
        "status": result.status, "reason": result.reason, "summary": result.summary,
        "spec": result.spec.model_dump() if result.spec else None,
        "app_version": result.app_version, "model": result.model,
        "steps": [{**asdict(s), "strategies": [x.model_dump() for x in s.strategies]}
                  for s in result.trace],
        "outputs": result.outputs,
    }


# ------------------------------------------------------------------ discover

@app.command()
def discover(
    goal: str = typer.Argument(..., help='e.g. "look up member 12345 and read their savings balance"'),
    app_id: str = typer.Option("legacy_core", "--app", help="App profile in config/apps/"),
    max_steps: int = typer.Option(25, help="Escalate after this many actions"),
    headed: bool = typer.Option(False, help="Show the browser window"),
    operator: bool = typer.Option(False, help="Start the operator console for human handoff"),
    save: bool = typer.Option(True, "--save/--no-save", help="Save the validated capability"),
    allow_irreversible: bool = typer.Option(
        False, help="Let discovery commit irreversible steps (test environments only)"),
    script: Path | None = typer.Option(
        None, help="Offline: take decisions from a YAML script instead of Gemini (no API key)"),
):
    """Let the LLM accomplish GOAL on the live app, then compile and validate a capability."""
    raise typer.Exit(asyncio.run(_discover(goal, app_id, max_steps, headed, operator, save,
                                           allow_irreversible, script)))


async def _discover(goal: str, app_id: str, max_steps: int, headed: bool, operator: bool,
                    save: bool, allow_irreversible: bool, script: Path | None) -> int:
    profile = load_profile(app_id)
    log = RunLog("discover", Redactor(profile.pii_patterns))
    typer.secho(f"Discovery run {log.run_id}  (evidence: {log.dir})", bold=True)
    model = "scripted" if script else DISCOVERY_MODEL
    log.event("run_started", f"goal: {goal} (model: {model})", goal=goal, app=app_id, model=model,
              allow_irreversible=allow_irreversible)
    try:
        result = await _run_agent(goal, profile, log, max_steps, headed, operator,
                                  allow_irreversible, script)
        color = typer.colors.GREEN if result.status == "done" else typer.colors.YELLOW
        typer.secho(f"\nDiscovery {result.status}: {result.reason}", fg=color, bold=True)
        if result.status != "done":
            return 3 if result.status == "escalated" else 1
        registry = Registry()
        try:
            cap = compile_capability(result, profile, log.run_id, registry.next_version(
                profile.id, _slug(result.spec.capability_name)))
        except CompileError as e:
            typer.secho(f"Could not compile a capability: {e}", fg=typer.colors.RED)
            return 1
        log.write_text("candidate.yaml", yaml.safe_dump(
            cap.model_dump(mode="json", exclude_none=True), sort_keys=False))
        log.event("compiled", f"{cap.ref}: {len(cap.steps)} steps, risk {cap.risk}",
                  capability=cap.ref)
    finally:
        log.close()  # also writes the deferred screens and prompts, fully redacted

    typer.secho(f"\nValidating {cap.ref} with a replay in a fresh browser (no LLM)...", bold=True)
    params = {i.name: i.value for i in result.spec.inputs}
    check = await _replay(cap, params, profile, headed=headed, stop_before_irreversible=True,
                          kind="validate")
    # A validation replay stops before the first irreversible step, so it can only confirm
    # the outputs read before that commit point.
    commit = next((i for i, s in enumerate(cap.steps) if s.risk == "irreversible"), len(cap.steps))
    before_commit = {s.output for s in cap.steps[:commit] if s.action == "extract"}
    expected = {k: parse_output(v["value"], cap.outputs[k]) for k, v in result.outputs.items()
                if k in before_commit}
    ok = check.status == "success" and all(check.outputs.get(k) == v for k, v in expected.items())
    if not ok:
        typer.secho(f"Validation failed ({check.status}); the candidate stays in {log.dir}/candidate.yaml",
                    fg=typer.colors.RED)
        _print_result(check)
        return 1
    cap.provenance.validated_by_run = check.run_id
    if not save:
        typer.secho(f"\nValidated {cap.ref}; not saved (--no-save). Candidate: {log.dir}/candidate.yaml",
                    fg=typer.colors.GREEN, bold=True)
        return 0
    path = registry.save(cap)
    typer.secho(f"\nSaved {cap.ref} as a draft: {path}", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"Review it, then: cua approve {cap.name}")
    return 0


async def _run_agent(goal: str, profile: AppProfile, log: RunLog, max_steps: int, headed: bool,
                     operator: bool, allow_irreversible: bool, script: Path | None) -> DiscoveryResult:
    async with browser_session(log, headed, operator) as (surface, controller):
        llm = ScriptedClient(script) if script else \
            GeminiClient(GEMINI_API_KEY, [DISCOVERY_MODEL, *FALLBACK_MODELS], log)
        agent = DiscoveryAgent(surface, Executor(surface, profile, log), llm, log,
                               max_steps=max_steps, handoff=controller,
                               allow_irreversible=allow_irreversible)
        try:
            result = await agent.run(goal)
        except Exception as e:  # noqa: BLE001 - top-level boundary: keep the evidence whatever broke
            log.event("error", f"{type(e).__name__}: {e}")
            result = agent.result
            result.status, result.reason = "failed", f"{type(e).__name__}: {e}"
        if result.status != "done":
            await surface.screenshot(log.path("failure.png"), log.redactor.sensitive_values())
        await surface.stop_trace(log.path("trace.zip") if result.status != "done" else None)
        log.write_json("trace.json", trace_json(result))
        return result


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
    allow_irreversible: bool = typer.Option(False, help="Approve irreversible steps for this run"),
    headed: bool = typer.Option(False, help="Show the browser window"),
    operator: bool = typer.Option(False, help="Start the operator console for human handoff"),
    as_json: bool = typer.Option(False, "--json", help="Print only the result JSON"),
):
    """Replay a capability deterministically, with no LLM."""
    try:
        cap = Registry().load(name, version)
    except LookupError as e:
        raise typer.BadParameter(str(e)) from e
    if cap.status != "approved" and not allow_draft:
        typer.secho(f"{cap.ref} is a {cap.status}. Approve it (cua approve {cap.name}) or pass "
                    "--allow-draft.", fg=typer.colors.RED)
        raise typer.Exit(1)
    profile = load_profile(cap.app.id)
    result = asyncio.run(_replay(cap, _parse_params(param or []), profile, headed=headed or operator,
                                 allow_irreversible=allow_irreversible, operator=operator,
                                 quiet=as_json))
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_result(result)
    raise typer.Exit(EXIT[result.status])


async def _replay(cap: Capability, params: dict[str, str], profile: AppProfile, *, headed: bool,
                  allow_irreversible: bool = False, stop_before_irreversible: bool = False,
                  operator: bool = False, kind: str = "replay", quiet: bool = False) -> RunResult:
    log = RunLog(kind, Redactor(profile.pii_patterns), quiet=quiet)
    if not quiet:
        typer.secho(f"Replay run {log.run_id}  (evidence: {log.dir})", bold=True)
    try:
        async with browser_session(log, headed, operator) as (surface, controller):
            engine = ReplayEngine(surface, profile, log, handoff=controller)
            return await engine.run(cap, params, allow_irreversible=allow_irreversible,
                                    stop_before_irreversible=stop_before_irreversible)
    finally:
        log.close()


def _print_result(r: RunResult) -> None:
    color = {"success": typer.colors.GREEN, "business_outcome": typer.colors.CYAN,
             "failed": typer.colors.RED, "escalated": typer.colors.MAGENTA}[r.status]
    typer.secho(f"\nResult: {r.status.upper()}", fg=color, bold=True)
    typer.echo(r.model_dump_json(indent=2, exclude_defaults=True))


# ------------------------------------------------------------------ catalog

@app.command()
def capabilities() -> None:
    """List saved capabilities."""
    caps = Registry().all()
    if not caps:
        typer.echo("No capabilities yet. Run `cua discover` first.")
        return
    for c in caps:
        inputs = ", ".join(f"{k}: {v.type}" for k, v in c.inputs.items())
        outputs = ", ".join(f"{k}: {v.type}" for k, v in c.outputs.items())
        typer.echo(f"{c.ref:<34} {c.status:<9} risk={c.risk:<12} ({inputs}) -> ({outputs})")
        typer.secho(f"    {c.description}", dim=True)


@app.command()
def show(name: str, version: str | None = None) -> None:
    """Print a capability's YAML."""
    reg = Registry()
    cap = reg.load(name, version)
    typer.echo(reg.path(cap).read_text())


@app.command()
def approve(name: str, version: str | None = None,
            by: str = typer.Option("reviewer", help="Who approved it")) -> None:
    """Mark a capability approved for unattended replay."""
    reg = Registry()
    cap = reg.load(name, version)
    if not cap.provenance.validated_by_run:
        typer.secho("Refusing: this version has no passing validation replay.", fg=typer.colors.RED)
        raise typer.Exit(1)
    cap.status = "approved"
    cap.provenance.approved_by = by
    cap.provenance.approved_at = datetime.now(UTC).replace(microsecond=0)
    reg.save(cap, overwrite=True)  # status and approval metadata only; the flow is unchanged
    typer.secho(f"{cap.ref} approved by {by}", fg=typer.colors.GREEN)



if __name__ == "__main__":
    app()
