"""Review of a capability: the plain-English card, approve / reject rules, and the catalog
the router chooses from."""

from datetime import UTC, datetime

import pytest

from ui_automation.catalog.capability_review import ReviewRefused, approve, reject, review_card
from ui_automation.catalog.capability_store import CapabilityStore
from ui_automation.models.capability import (
    InputSpec,
    LabelAnchor,
    Provenance,
    RoleName,
    RowMatch,
    Step,
    TableCell,
    Target,
)
from ui_automation.web.control_room import catalog

from .capability_examples import make_capability


def validated(**overrides):
    return make_capability(provenance=Provenance(recorded_at=datetime.now(UTC),
                                                 validated_by_run="validate-1"), **overrides)


def test_card_reads_like_instructions():
    cap = make_capability(
        risk="irreversible",
        inputs={"member_id": InputSpec(pattern=r"^\d{1,12}$"), "note": InputSpec()},
        steps=[
            Step(id="enter", intent="search", action="fill", value="${member_id}",
                 target=Target(frame="main", strategies=[LabelAnchor(label="Member Number", role="textbox")])),
            Step(id="confirm", intent="commit", action="click", risk="irreversible",
                 target=Target(strategies=[RoleName(role="button", name="Confirm")])),
            Step(id="read", intent="read it", action="extract", output="savings_balance",
                 target=Target(strategies=[TableCell(row=RowMatch(column="Description", equals="REGULAR SAVINGS"),
                                                     column="Balance")])),
        ])
    card = review_card(cap)
    does = [s["does"] for s in card["steps"]]
    assert does[0] == "Type the member id from the request into the field next to 'Member Number' in the main area."
    assert does[1] == "Click the 'Confirm' button."
    assert does[2] == ("Read the Balance of the row whose Description is 'REGULAR SAVINGS' "
                       "and give it back as the savings balance.")
    assert "can't be undone" in card["steps"][1]["risk_note"].lower()
    assert "Input 'note' accepts any text (no format check)." in card["warnings"]
    assert not card["can_review"]  # never validated by a replay


def test_approve_and_reject_rules(tmp_path):
    store = CapabilityStore(tmp_path)
    store.save(make_capability())  # draft, never validated
    with pytest.raises(ReviewRefused, match="no passing validation replay"):
        approve(store, "get_savings_balance", "kim")

    store.save(validated(version="1.1.0"))
    with pytest.raises(ReviewRefused, match="say why"):
        reject(store, "get_savings_balance", "kim", "  ")
    cap = approve(store, "get_savings_balance", "kim")
    assert (cap.version, cap.status, cap.provenance.approved_by) == ("1.1.0", "approved", "kim")
    assert store.load("get_savings_balance", "1.1.0").status == "approved"  # written to disk

    store.save(validated(version="1.2.0"))
    cap = reject(store, "get_savings_balance", "lee", "reads the wrong row")
    assert (cap.status, cap.provenance.rejection_reason) == ("rejected", "reads the wrong row")
    with pytest.raises(ReviewRefused, match="only a draft"):
        approve(store, "get_savings_balance", "kim", version="1.2.0")


def test_a_new_draft_does_not_hide_the_approved_version(tmp_path, monkeypatch):
    store = CapabilityStore(tmp_path)
    store.save(validated(status="approved"))
    store.save(validated(version="1.1.0"))  # a newer draft waiting for review
    monkeypatch.setattr("ui_automation.web.control_room.CapabilityStore", lambda: store)
    listed = {(c.version, c.status) for c in catalog()}
    assert listed == {("1.0.0", "approved"), ("1.1.0", "draft")}  # the router still sees 1.0.0


def test_steps_are_told_back_to_the_requester_in_plain_words():
    from ui_automation.catalog.capability_review import done_sentence

    fill = Step(id="enter", intent="search", action="fill", value="${member_id}",
                target=Target(strategies=[LabelAnchor(label="Member Number", role="textbox")]))
    read = Step(id="read", intent="read it", action="extract", output="savings_balance",
                target=Target(strategies=[TableCell(row=RowMatch(column="Description", equals="REGULAR SAVINGS"),
                                                    column="Current Balance")]))
    assert done_sentence(fill, {"member_id": "23456"}) == "Typed 23456 into 'Member Number'"
    assert done_sentence(read, {}) == "Read the Current Balance for REGULAR SAVINGS"
