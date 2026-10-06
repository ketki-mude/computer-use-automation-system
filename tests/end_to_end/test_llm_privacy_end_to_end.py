"""Proof of the privacy rule. A full discovery runs on the mock bank (only the AI's decisions
are scripted; browser, sign-on, recorder, compiler, validation replay and save are real).
No member data or credential may reach the model, and none may be written to any file."""

import asyncio
import json
import re

import pytest

from ui_automation import capability_workflows
from ui_automation.catalog.capability_store import CapabilityStore
from ui_automation.discovery.llm_clients import ScriptedClient
from ui_automation.settings import SCRIPTED_DISCOVERY_DIR

pytestmark = pytest.mark.integration

# Member 23456 on the mock bank, another member in the same search, and the teller's sign-on.
SENSITIVE = {
    "member number": "23456", "savings balance": "15,002.75", "savings balance, parsed": "15002.75",
    "checking balance": "820.40", "name": "ROBERT DEMO", "address": "450 TEST BLVD",
    "account number": "23456-S0000", "other member's number": "123456",
    "other member's name": "JOHN R EXAMPLE", "password": "Demo#2026", "user id": "teller01",
}


def leaks(text: str) -> list[str]:
    """Which sensitive values appear in `text` as whole tokens."""
    return [what for what, value in SENSITIVE.items()
            if re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", text)]


class RecordingLLM:
    """Passes calls to the scripted decisions and keeps everything the model was shown."""

    def __init__(self, inner: ScriptedClient):
        self.inner = inner
        self.shown: list[str] = []

    async def structured(self, system, prompt, schema):
        self.shown.append(system + prompt)
        return await self.inner.structured(system, prompt, schema)

    async def call_tool(self, system, prompt, tools, image=None):
        self.shown.append(system + prompt + json.dumps(tools))
        return await self.inner.call_tool(system, prompt, tools, image)


def test_no_member_data_reaches_the_model_or_any_file(env, tmp_path):
    profile, policy = env
    store = CapabilityStore(tmp_path / "capabilities")
    llm = RecordingLLM(ScriptedClient(SCRIPTED_DISCOVERY_DIR / "get_savings_balance.yaml"))
    log = capability_workflows.new_run_log("discover", profile, quiet=True)

    outcome = asyncio.run(capability_workflows.learn_capability(
        "What's the savings balance for member 23456?", profile, log, llm, policy=policy,
        store=store))

    # Code got the answer and the capability was learned, validated and saved...
    assert outcome.ok and outcome.saved_to, outcome.message
    assert outcome.validation.outputs == {"savings_balance": "15002.75"}
    # ...while the model worked only with placeholders.
    assert any("{member_number}" in s for s in llm.shown)
    for n, shown in enumerate(llm.shown):
        assert not leaks(shown), f"model call {n} was shown {leaks(shown)}"
    # Nothing sensitive in any file: events, observations, recorded steps, candidate, result, YAML.
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) > 5
    for path in written:
        assert path.suffix not in (".png", ".zip"), f"unexpected binary artifact {path.name}"
        found = leaks(path.read_text(encoding="utf-8"))
        assert not found, f"{path.relative_to(tmp_path)} contains {found}"
