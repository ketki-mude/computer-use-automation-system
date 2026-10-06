"""Human review of a capability: a plain-English card, and the approve / reject decisions.

A reviewer should not have to read YAML to decide. The card says what the capability does,
what it needs and returns, each step as a sentence, which steps change data, the business
answers a caller must handle, and where the recipe came from. Approve and reject live here
so the command line and the control room apply exactly the same rules.
"""

import re
from datetime import UTC, datetime

from ..models.capability import Capability, Step, Target
from .capability_store import CapabilityStore

PARAM_REF = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")
ROLE_WORDS = {"textbox": "field", "combobox": "drop-down", "checkbox": "checkbox",
              "link": "link", "button": "button", "cell": "value"}
RISK_NOTES = {
    "reversible": "Changes something in the app, but it can be undone.",
    "irreversible": ("Can't be undone, so the automation stops here until a staff member approves "
                     "it (only a test environment may allow otherwise)."),
}


class ReviewRefused(Exception):
    """The capability's current state does not allow this review decision."""


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _newest_draft(store: CapabilityStore, name: str) -> Capability:
    drafts = [c for c in store.all() if c.name == name and c.status == "draft"]
    if not drafts:
        raise LookupError(f"no draft of {name} is waiting for review")
    return drafts[-1]


def approve(store: CapabilityStore, name: str, by: str, version: str | None = None) -> Capability:
    """Approve a draft for unattended replay. Only a draft with a passing validation replay
    qualifies. Status and approval fields change; the flow itself never does."""
    cap = store.load(name, version) if version else _newest_draft(store, name)
    if cap.status != "draft":
        raise ReviewRefused(f"{cap.ref} is {cap.status}; only a draft can be approved")
    if not cap.provenance.validated_by_run:
        raise ReviewRefused(f"{cap.ref} has no passing validation replay")
    cap.status = "approved"
    cap.provenance.approved_by = by
    cap.provenance.approved_at = _now()
    store.save(cap, overwrite=True)
    return cap


def reject(store: CapabilityStore, name: str, by: str, reason: str,
           version: str | None = None) -> Capability:
    """Reject a draft. It stays on disk for the audit trail but is never offered to the router;
    the next discovery of the same task becomes a new version."""
    cap = store.load(name, version) if version else _newest_draft(store, name)
    if cap.status != "draft":
        raise ReviewRefused(f"{cap.ref} is {cap.status}; only a draft can be rejected")
    if not reason.strip():
        raise ReviewRefused("say why it is rejected, so the next discovery can do better")
    cap.status = "rejected"
    cap.provenance.rejected_by = by
    cap.provenance.rejected_at = _now()
    cap.provenance.rejection_reason = reason.strip()
    store.save(cap, overwrite=True)
    return cap


def _value_text(raw: str) -> str:
    """'${member_number}' -> 'the member number from the request'; a literal stays quoted."""
    if m := PARAM_REF.fullmatch(raw):
        return f"the {m.group(1).replace('_', ' ')} from the request"
    return "'" + PARAM_REF.sub(lambda m: f"[{m.group(1).replace('_', ' ')}]", raw) + "'"


def target_text(target: Target) -> str:
    """The first locator strategy, in words a teller would use."""
    s = target.strategies[0]
    if s.kind == "role_name":
        text = f"the '{s.name}' {ROLE_WORDS.get(s.role, s.role)}"
    elif s.kind == "label_anchor":
        text = f"the {ROLE_WORDS.get(s.role, s.role)} next to '{s.label}'"
    elif s.kind == "table_row":
        text = f"'{s.name}' in the row whose {s.row.column} is {_value_text(s.row.equals)}"
    elif s.kind == "table_cell":
        text = f"the {s.column} of the row whose {s.row.column} is {_value_text(s.row.equals)}"
    elif s.kind == "text":
        text = f"the text '{s.text}'"
    else:
        text = f"the element matching {s.selector}"
    return f"{text} in the {target.frame} area" if target.frame else text


def step_sentence(step: Step) -> str:
    where = target_text(step.target)
    if step.action == "click":
        return f"Click {where}."
    if step.action == "fill":
        return f"Type {_value_text(step.value or '')} into {where}."
    if step.action == "select":
        return f"Choose {_value_text(step.value or '')} in {where}."
    return f"Read {where} and give it back as the {step.output.replace('_', ' ')}."


def done_sentence(step: Step, params: dict[str, str]) -> str:
    """What a step did, in the past tense and with the request's own values, for the person
    who asked ("Typed 23456 into 'Member Number'")."""
    def fill(raw: str | None) -> str:
        return PARAM_REF.sub(lambda m: params.get(m.group(1), m.group(1)), raw or "")

    s = step.target.strategies[0]
    label = getattr(s, "name", None) or getattr(s, "label", None) or getattr(s, "text", None) or ""
    if s.kind in ("table_row", "table_cell"):
        row = fill(s.row.equals)
        if step.action == "extract":
            return f"Read the {s.column} for {row}" if s.kind == "table_cell" else f"Read the value for {row}"
        return f"Clicked '{s.name}' on the row for {row}" if s.kind == "table_row" else f"Clicked the entry for {row}"
    if step.action == "click":
        return f"Clicked '{label}'" if label else "Clicked"
    if step.action == "fill":
        return f"Typed {fill(step.value)} into '{label}'" if label else f"Typed {fill(step.value)}"
    if step.action == "select":
        return f"Chose {fill(step.value)} for '{label}'" if label else f"Chose {fill(step.value)}"
    return f"Read the {label}" if label else f"Read the {step.output.replace('_', ' ')}"


def _check_sentence(step: Step) -> str | None:
    if step.expect is None:
        return None
    if step.expect.title_contains:
        return f"Then the screen title must contain {_value_text(step.expect.title_contains)}."
    if step.expect.text:
        return f"Then the screen must show {_value_text(step.expect.text)}."
    return None


def _warnings(cap: Capability) -> list[str]:
    notes = []
    if not cap.provenance.validated_by_run:
        notes.append("It hasn't passed its test runs yet, so it can't be approved.")
    for step in cap.steps:
        if all(s.kind == "css" for s in step.target.strategies):
            notes.append(f"Step '{step.id}' can only be found by a page selector; a screen "
                         "redesign may break it.")
        if step.provenance == "human":
            notes.append(f"Step '{step.id}' was performed by a person during discovery.")
    for name, spec in cap.inputs.items():
        if spec.type == "string" and spec.pattern is None:
            notes.append(f"Input '{name}' accepts any text (no format check).")
    return notes


def _readable(text: str) -> str:
    """An intent the model wrote, with ${member_number} shown as [member number]."""
    return PARAM_REF.sub(lambda m: f"[{m.group(1).replace('_', ' ')}]", text)


def review_card(cap: Capability) -> dict:
    """Everything a reviewer needs to approve or reject, without reading YAML."""
    by_kind: dict[str, list[dict]] = {}
    for code, rule in cap.outcomes.items():
        by_kind.setdefault(rule.kind, []).append(
            {"code": code, "meaning": rule.description or rule.message or code})
    p = cap.provenance
    return {
        "name": cap.name, "version": cap.version, "ref": cap.ref, "status": cap.status,
        "risk": cap.risk, "risk_note": RISK_NOTES.get(cap.risk, "Only reads information; it changes nothing."),
        "description": cap.description,
        "inputs": [{"name": k, "type": v.type, "description": v.description,
                    "format": v.pattern, "sensitivity": v.sensitivity} for k, v in cap.inputs.items()],
        "outputs": [{"name": k, "type": v.type, "description": v.description,
                     "sensitivity": v.sensitivity} for k, v in cap.outputs.items()],
        "steps": [{"id": s.id, "why": _readable(s.intent), "does": step_sentence(s), "check": _check_sentence(s),
                   "risk": s.risk, "risk_note": RISK_NOTES.get(s.risk), "by": s.provenance,
                   "ways_to_find": len(s.target.strategies)} for s in cap.steps],
        "business_outcomes": [{"code": k, "meaning": v} for k, v in cap.business_outcomes.items()],
        "handled_by_itself": by_kind.get("recoverable", []),
        "stops_with_error": by_kind.get("failure", []),
        "asks_a_person": by_kind.get("escalate", []),
        "provenance": {
            "discovered_by_run": p.discovered_by_run,
            "recorded_at": p.recorded_at.isoformat(), "validated_by_run": p.validated_by_run,
            "validation_runs": p.validation_runs or ([p.validated_by_run] if p.validated_by_run else []),
            "approved_by": p.approved_by, "approved_at": p.approved_at and p.approved_at.isoformat(),
            "rejected_by": p.rejected_by, "rejection_reason": p.rejection_reason},
        "warnings": _warnings(cap),
        "can_review": cap.status == "draft" and bool(p.validated_by_run),
    }
