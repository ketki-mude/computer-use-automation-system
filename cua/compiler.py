"""Compile a discovery trace into a capability artifact.

What the compiler decides, in order:
1. prune: drop failed actions and superseded fills; keep the path that worked;
2. parameterize: every occurrence of an input's example value ("12345") in values,
   locators, intents and postconditions becomes a reference (${member_id});
3. generalize locators: if a row was found by an input, drop sibling row keys that are
   just this record's data (a member's name would only ever match this member, and
   would persist PII in the artifact);
4. postconditions: a frame title that changed after a step becomes that step's `expect`;
5. contract: inputs from the parsed goal, outputs from extract steps (money is typed as
   decimal), success checkpoint from the final screen.

The result is a draft. A validation replay must pass before it is saved.
"""

import re
from datetime import UTC, datetime

from pydantic import TypeAdapter

from .agent import DiscoveryResult, TraceStep
from .schema import (
    RISK_ORDER,
    AppProfile,
    AppRef,
    Capability,
    Expect,
    Fingerprint,
    InputSpec,
    OutputSpec,
    Provenance,
    Step,
    Strategy,
    SuccessCheckpoint,
    Target,
)

STRATEGY = TypeAdapter(Strategy)
MONEY = re.compile(r"^\(?-?\$?\s?[\d,]+\.\d{2}\)?$")
# "(min $5.00)", "($820.40 avail)": amounts inside an option label are data, not identity
MONEY_NOTE = re.compile(r"\s*\([^)]*\$[\d,.]+[^)]*\)")
ROW_KINDS = ("table_row", "table_cell")


class CompileError(Exception):
    pass


def parameterize(text: str, params: dict[str, str]) -> str:
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if value:
            text = re.sub(rf"(?<![\w]){re.escape(value)}(?![\w])", "${" + name + "}", text)
    return text


def _param_obj(obj, params: dict[str, str]):
    if isinstance(obj, str):
        return parameterize(obj, params)
    if isinstance(obj, dict):
        return {k: _param_obj(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_param_obj(v, params) for v in obj]
    return obj


def generalize(strategies: list[Strategy], params: dict[str, str]) -> list[Strategy]:
    """Parameterize every strategy. If the row was picked by an input (the primary row key is
    a ${param}), other literal row keys are just this record's data, e.g. the member's name:
    they would match only this member and would persist PII, so they are dropped. A literal
    primary key such as Description = REGULAR SAVINGS is a stable label and is kept."""
    dumped = [_param_obj(s.model_dump(), params) for s in strategies]
    rows = [d for d in dumped if d["kind"] in ROW_KINDS]
    if rows and "${" in repr(rows[0]):
        dumped = [d for d in dumped if d["kind"] not in ROW_KINDS or "${" in repr(d)]
    return [STRATEGY.validate_python(d) for d in dumped]


def prune(trace: list[TraceStep]) -> list[TraceStep]:
    kept: list[TraceStep] = []
    for t in (t for t in trace if t.ok):
        if kept and t.action == "fill" and kept[-1].action == "fill" and \
                kept[-1].element.get("ref") == t.element.get("ref"):
            kept[-1] = t  # a later fill of the same field replaces the earlier one
            continue
        kept.append(t)
    # keep only the last extract per output name
    last = {t.output["output_name"]: t.n for t in kept if t.action == "extract"}
    return [t for t in kept if t.action != "extract" or last[t.output["output_name"]] == t.n]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "step"


def _step_id(t: TraceStep, used: set[str]) -> str:
    el = t.element
    label = el.get("label") or el.get("name") or el.get("column") or el.get("role", "")
    base = {"fill": "enter_", "select": "choose_", "click": "click_"}.get(t.action, "") + _slug(label)
    if t.action == "extract":
        base = "read_" + _slug(t.output["output_name"])
    sid, n = base, 2
    while sid in used:
        sid, n = f"{base}_{n}", n + 1
    used.add(sid)
    return sid


def _expect(t: TraceStep, params: dict[str, str]) -> Expect | None:
    changed = {f: title for f, title in t.after.items() if title and t.before.get(f) != title}
    if not changed:
        return None
    frame = "main" if "main" in changed else next(iter(changed))
    return Expect(frame=None if frame == "top" else frame,
                  title_contains=parameterize(changed[frame], params))


def _value(t: TraceStep, params: dict[str, str]) -> str | None:
    if t.value is None:
        return None
    value = parameterize(t.value, params)
    if t.action == "select":
        value = MONEY_NOTE.sub("", value).strip()
    return value


def _fingerprint(el: dict) -> Fingerprint:
    if el.get("kind") == "cell":  # a value cell: its text is data, so record the column instead
        return Fingerprint(tag=el["tag"], role=el["role"], label=el.get("column", ""))
    return Fingerprint(tag=el["tag"], role=el["role"], label=el.get("label", ""),
                       text=el.get("name", "")[:60])


def _output_spec(t: TraceStep) -> OutputSpec:
    value = (t.extracted or "").strip()
    out = t.output
    if MONEY.match(value) or out.get("output_type") == "decimal":
        typ, parse = "decimal", "currency"
    elif re.fullmatch(r"-?[\d,]+", value) and out.get("output_type") == "integer":
        typ, parse = "integer", "integer"
    else:
        typ, parse = "string", "text"
    return OutputSpec(type=typ, parse=parse, description=out.get("description") or "",
                      sensitivity=out.get("sensitivity") or "internal")


def _input_spec(inp) -> InputSpec:
    typ = inp.type
    pattern = None
    # Identifiers are strings even when they look numeric (leading zeros matter).
    if re.search(r"(_id|_number|_no)$", inp.name) or typ == "integer" and inp.value.startswith("0"):
        typ = "string"
    if re.fullmatch(r"\d+", inp.value):
        pattern = r"^\d{1,12}$"
    return InputSpec(type=typ, description=inp.description, pattern=pattern,
                     sensitivity=inp.sensitivity)


def compile_capability(result: DiscoveryResult, profile: AppProfile, run_id: str,
                       version: str = "1.0.0") -> Capability:
    if result.status != "done" or result.spec is None:
        raise CompileError(f"discovery did not finish ({result.status}: {result.reason})")
    if result.human_steps:
        raise CompileError(f"a person performed {result.human_steps} action(s) mid-flow; they are "
                           "in the evidence but are not compiled into steps")
    spec = result.spec
    params = {i.name: i.value for i in spec.inputs}
    used: set[str] = set()
    steps: list[Step] = []
    outputs: dict[str, OutputSpec] = {}
    for t in prune(result.trace):
        strategies = generalize(t.strategies, params)
        if not strategies:
            raise CompileError(f"action {t.n} ({t.action}) has no locator that was proven unique")
        step = Step(
            id=_step_id(t, used),
            intent=parameterize(t.reason, params) or t.action,
            action=t.action,
            target=Target(frame=t.frame, strategies=strategies, fingerprint=_fingerprint(t.element)),
            value=_value(t, params),
            output=t.output["output_name"] if t.action == "extract" else None,
            risk=t.risk,
            expect=_expect(t, params),
        )
        if step.action == "extract":
            outputs[step.output] = _output_spec(t)
        steps.append(step)
    if not steps:
        raise CompileError("discovery recorded no successful actions")

    final = result.final_titles
    frame = "main" if "main" in final else next(iter(final), None)
    success = SuccessCheckpoint(
        frame=frame if frame and frame != "top" else None,
        title_contains=parameterize(final[frame], params) if frame else None,
        outputs_present=bool(outputs),
    )
    version_range = "*"
    if result.app_version and (m := re.match(r"(\d+)\.(\d+)", result.app_version)):
        version_range = f"{m.group(1)}.{m.group(2)}.*"
    worst = max((RISK_ORDER[s.risk] for s in steps), default=0)
    return Capability(
        name=_slug(spec.capability_name),
        version=version,
        status="draft",
        description=spec.description,
        app=AppRef(id=profile.id, vendor=profile.vendor, versions=version_range),
        risk=next(k for k, v in RISK_ORDER.items() if v == worst),
        inputs={i.name: _input_spec(i) for i in spec.inputs},
        outputs=outputs,
        steps=steps,
        success=success,
        provenance=Provenance(discovered_by_run=run_id, model=result.model,
                              recorded_at=datetime.now(UTC).replace(microsecond=0)),
    )
