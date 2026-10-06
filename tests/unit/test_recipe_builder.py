"""Recipe builder: parameterizing values and generalizing locators."""

from ui_automation.discovery.recipe_builder import contract_outcomes, generalize, parameterize
from ui_automation.models.app_profile import AppProfile
from ui_automation.models.capability import LabelAnchor, RoleName, RowMatch, TableCell, TableRow

PARAMS = {"member_id": "12345"}


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


def test_irreversible_contract_lists_rejection():
    profile = AppProfile.model_validate({
        "id": "a", "vendor": "v", "base_url": "http://x", "login": [
            {"id": "s", "intent": "i", "action": "click",
             "target": {"strategies": [{"kind": "role_name", "role": "button", "name": "Go"}]}}],
        "signed_in": {"frame_present": "main"},
        "screens": {"RECORD_NOT_FOUND": {"kind": "business", "when": {"text": "No records"},
                                         "description": "No member matches"}}})
    assert set(contract_outcomes(profile, irreversible=False)) == {"RECORD_NOT_FOUND"}
    assert "REJECTED_BY_OPERATOR" in contract_outcomes(profile, irreversible=True)
