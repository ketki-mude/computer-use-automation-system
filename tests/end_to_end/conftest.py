"""The mock bank, in this process on its own port, so tests can set faults and read its audit."""

import threading
import time

import pytest
import uvicorn

from mock_bank import bank_app, fault_injection, sample_members
from ui_automation import evidence_writer
from ui_automation.config_files import load_app_profile
from ui_automation.safety.safety_policy import SafetyPolicy
from ui_automation.settings import CONFIG_VALUES

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
SERVER_START_POLL_S = 0.05
SERVER_STOP_TIMEOUT_S = 5


@pytest.fixture(scope="session")
def bank_server():
    server = uvicorn.Server(uvicorn.Config(bank_app.app, host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(SERVER_START_POLL_S)
    yield
    server.should_exit = True
    thread.join(SERVER_STOP_TIMEOUT_S)


@pytest.fixture
def env(bank_server, tmp_path, monkeypatch):
    """A clean bank, runs written under tmp_path, and a profile and policy aimed at the test port."""
    sample_members.reset()
    fault_injection.reset()
    bank_app.SESSIONS.clear()
    bank_app.PENDING.clear()
    bank_app.AUDIT.update(last_otp=None, opened=[], closed=[])
    monkeypatch.setattr(evidence_writer, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setitem(CONFIG_VALUES, "BANK_URL", BASE)  # profile, allowlist, ticket text
    return load_app_profile("acmecore_teller"), SafetyPolicy.load()
