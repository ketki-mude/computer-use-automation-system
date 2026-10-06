"""Log masking: registered values, patterns, non-ASCII text, personal-data fields."""

import json

from ui_automation.safety.log_masking import Redactor, personal_data_values
from ui_automation.screen.screen_interface import Observation


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


def test_redaction_sees_non_ascii_values_in_logs():
    r = Redactor()
    r.register("José Núñez", "pii")
    assert "Núñez" not in r.text(json.dumps({"p": "member José Núñez"}, ensure_ascii=False))


def test_values_in_pii_fields_are_found_by_label_and_by_column():
    obs = Observation(text="", frames={}, elements={
        1: {"kind": "cell", "text": "Member Name:", "row_texts": ["Member Name:", "JANE Q SAMPLE"], "pos": 0},
        2: {"kind": "cell", "text": "JANE Q SAMPLE", "row_texts": ["Member Name:", "JANE Q SAMPLE"], "pos": 1},
        3: {"kind": "cell", "text": "JOHN R EXAMPLE", "column": "Name", "header": ["Member #", "Name"],
            "row_texts": ["123456", "JOHN R EXAMPLE"], "pos": 1},
        4: {"kind": "cell", "text": "$88.10", "column": "Balance", "header": ["Balance"], "pos": 0},
    })
    assert personal_data_values(obs, ["Member Name", "Name"]) == ["JANE Q SAMPLE", "JOHN R EXAMPLE"]


def test_column_titles_are_never_taken_for_personal_data():
    # "Branch" sits after the "Name" title in the header row; it is a title, not a name.
    header = ["Member #", "Name", "Branch"]
    obs = Observation(text="", frames={}, elements={
        i: {"kind": "cell", "text": t, "row_texts": header, "pos": i, "is_header": True}
        for i, t in enumerate(header)})
    assert personal_data_values(obs, ["Name"]) == []
