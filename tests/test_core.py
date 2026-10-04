"""Unit tests for the decisions that make replay safe and deterministic."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cua.compiler import generalize, parameterize
from cua.policy import Policy
from cua.redact import Redactor
from cua.replay import parse_output, validate_inputs
from cua.schema import (
    AppRef,
    Capability,
    InputSpec,
    LabelAnchor,
    OutputSpec,
    Provenance,
    RoleName,
    RowMatch,
    Step,
    SuccessCheckpoint,
    TableCell,
    TableRow,
    Target,
)

PARAMS = {"member_id": "12345"}


# ---------------------------------------------------------------- compiler

def test_parameterize_whole_tokens_only():
    assert parameterize("Member Inquiry - 12345", PARAMS) == "Member Inquiry - ${member_id}"
    assert parameterize("12345-S0000", PARAMS) == "${member_id}-S0000"
    assert parameterize("member 123456", PARAMS) == "member 123456"  # not a partial match


def test_row_found_by_input_drops_other_record_data():
    # The View link's row was found by Member # = 12345; the member's name is record data
    # (only matches this member, and is PII), so it must not survive into the artifact.
    strategies = [
        TableRow(row=RowMatch(column="Member #", equals="12345"), role="link", name="View"),
        TableRow(row=RowMatch(column="Name", equals="JANE Q SAMPLE"), role="link", name="View"),
    ]
    out = generalize(strategies, PARAMS)
    assert len(out) == 1
    assert out[0].row.equals == "${member_id}"


def test_stable_label_row_key_is_kept_with_parameterized_fallback():
    strategies = [
        TableCell(row=RowMatch(column="Description", equals="REGULAR SAVINGS"), column="Current Balance"),
        TableCell(row=RowMatch(column="Account Number", equals="12345-S0000"), column="Current Balance"),
    ]
    out = generalize(strategies, PARAMS)
    assert [s.row.equals for s in out] == ["REGULAR SAVINGS", "${member_id}-S0000"]


def test_non_row_strategies_are_never_dropped():
    strategies = [LabelAnchor(label="Member Number", role="textbox"),
                  RoleName(role="button", name="Search")]
    assert len(generalize(strategies, PARAMS)) == 2


# ---------------------------------------------------------------- policy

@pytest.fixture
def policy() -> Policy:
    return Policy.load()


def test_allowlist_routes(policy):
    assert policy.allows_url("http://127.0.0.1:8000/app/search")[0]
    assert not policy.allows_url("http://127.0.0.1:8000/app/member/close?mbr=1")[0]
    assert not policy.allows_url("http://127.0.0.1:8000/__admin/reset")[0]
    assert not policy.allows_url("https://evil.example.com/app/search")[0]


def test_risk_classes(policy):
    assert policy.classify("click", "button", "Confirm") == "irreversible"
    assert policy.classify("click", "button", "Confirm Close") == "irreversible"
    assert policy.classify("click", "button", "Search") == "read"
    assert policy.classify("click", "link", "Member Search") == "read"
    assert policy.classify("click", "button", "Continue") == "reversible"
    assert policy.classify("fill", "textbox", "") == "read"
    assert policy.classify("select", "combobox", "", onchange="__doPostBack()") == "reversible"


def test_javascript_navigation_is_checked_before_clicking(policy):
    # The "Close Membership" button navigates by JavaScript, not by a link.
    meta = {"role": "button", "name": "Close Membership",
            "onclick": "location.href='/app/member/close?mbr=31337'"}
    decision = policy.authorize("click", meta, "http://127.0.0.1:8000/app/member?mbr=31337")
    assert not decision.allowed
    assert "denied" in decision.reason


# ---------------------------------------------------------------- redaction

def test_redaction_layers():
    r = Redactor([r"\b\d{5,10}-S\d{4}\b"])
    r.register("12345", "pii")
    r.register("Demo#2026", "secret")
    r.register("ok", "internal")  # not sensitive: left alone
    out = r.text("member 12345 / 123456, acct 12345-S0000, ssn 123-45-6789, pw Demo#2026, ok")
    assert "12345 " not in out and "***45" in out
    assert "123456" in out  # a different value is untouched
    assert "12345-S0000" not in out and "123-45-6789" not in out and "Demo#2026" not in out
    assert out.endswith(", ok")


def test_redaction_leaves_identifiers_alone():
    r = Redactor()
    assert r.text("run discover-20261004-000523-1db1") == "run discover-20261004-000523-1db1"
    assert r.text("get_savings_balance@1.0.0") == "get_savings_balance@1.0.0"


# ---------------------------------------------------------------- replay contract

def _capability(**overrides) -> Capability:
    fields = {
        "name": "get_savings_balance", "version": "1.0.0", "description": "Read a balance.",
        "app": AppRef(id="legacy_core", vendor="acmecore_teller"), "risk": "read",
        "inputs": {"member_id": InputSpec(pattern=r"^\d{1,12}$", sensitivity="pii")},
        "outputs": {"savings_balance": OutputSpec(type="decimal", parse="currency")},
        "steps": [Step(id="read", intent="read it", action="extract", output="savings_balance",
                    target=Target(strategies=[RoleName(role="cell", name="x")]))],
        "success": SuccessCheckpoint(), "provenance": Provenance(recorded_at=datetime.now(UTC)),
    }
    fields.update(overrides)
    return Capability(**fields)


def test_input_validation():
    cap = _capability()
    assert validate_inputs(cap, {"member_id": "12345"}) == []
    assert validate_inputs(cap, {"member_id": "abc"}) == ["member_id does not match ^\\d{1,12}$"]
    assert "missing input member_id" in validate_inputs(cap, {})
    assert "unknown input other" in validate_inputs(cap, {"member_id": "1", "other": "x"})


def test_money_is_parsed_exactly():
    spec = OutputSpec(type="decimal", parse="currency")
    assert parse_output("$1,204.50", spec) == "1204.50"
    assert parse_output("($42.10)", spec) == "-42.10"


def test_capability_invariants():
    with pytest.raises(ValidationError, match="never extracted"):
        _capability(outputs={"savings_balance": OutputSpec(), "other": OutputSpec()})
    with pytest.raises(ValidationError, match="riskiest step"):
        _capability(steps=[Step(id="go", intent="commit", action="click", risk="irreversible",
                                target=Target(strategies=[RoleName(role="button", name="Confirm")])),
                           Step(id="read", intent="read", action="extract", output="savings_balance",
                                target=Target(strategies=[RoleName(role="cell", name="x")]))])
