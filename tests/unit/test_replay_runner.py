"""Replay contract helpers: input validation and exact money parsing."""

import asyncio

from ui_automation import evidence_writer
from ui_automation.config_files import load_app_profile
from ui_automation.evidence_writer import RunLog
from ui_automation.models.capability import InputSpec, OutputSpec
from ui_automation.replay.replay_runner import ReplayRunner, parse_output, validate_inputs
from ui_automation.safety.log_masking import Redactor

from .capability_examples import make_capability


def test_input_validation():
    cap = make_capability()
    assert validate_inputs(cap, {"member_id": "12345"}) == []
    assert validate_inputs(cap, {"member_id": "abc"}) == ["member_id does not match ^\\d{1,12}$"]
    assert "missing input member_id" in validate_inputs(cap, {})
    assert "unknown input other" in validate_inputs(cap, {"member_id": "1", "other": "x"})


def test_digits_mean_ascii_digits():
    cap = make_capability()
    assert validate_inputs(cap, {"member_id": "١٢٣٤٥"})  # Arabic-Indic digits are rejected
    assert validate_inputs(cap, {"member_id": "12345"}) == []


def test_money_is_parsed_exactly():
    spec = OutputSpec(type="decimal", parse="currency")
    assert parse_output("$1,204.50", spec) == "1204.50"
    assert parse_output("($42.10)", spec) == "-42.10"


def test_decimal_inputs_must_be_plain_numbers():
    cap = make_capability(inputs={"amount": InputSpec(type="decimal")})
    assert validate_inputs(cap, {"amount": "100.00"}) == []
    for bad in ("NaN", "Infinity", "1e3", "100.", "1,000.00"):  # Decimal() alone accepts the first four
        assert validate_inputs(cap, {"amount": bad}) == ["amount must be a decimal number"], bad


class NoBrowser:
    """Enough of a screen for a run that stops before opening the app."""
    page = None

    def __init__(self):
        self.dialogs: list = []

    async def stop_trace(self, path):
        return None


def test_an_undeclared_input_is_masked_in_the_log(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_writer, "RUNS_DIR", tmp_path)
    profile = load_app_profile("acmecore_teller")
    log = RunLog("test", Redactor(), quiet=True)
    runner = ReplayRunner(NoBrowser(), profile, log)
    result = asyncio.run(runner.run(make_capability(), {"member_number": "23456"}))
    log.close()
    assert result.error.kind == "INPUT_INVALID"
    assert "23456" not in (log.dir / "events.jsonl").read_text(encoding="utf-8")
    assert "23456" not in (log.dir / "result.json").read_text(encoding="utf-8")
