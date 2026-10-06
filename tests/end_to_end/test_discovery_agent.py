"""Discovery loop behaviour on the live mock bank (decisions scripted)."""

import asyncio

import pytest
import yaml

from ui_automation import capability_workflows
from ui_automation.catalog.capability_store import CapabilityStore
from ui_automation.discovery.llm_clients import ScriptedClient
from ui_automation.settings import SCRIPTED_DISCOVERY_DIR

pytestmark = pytest.mark.integration


def test_re_reading_a_captured_value_is_skipped_not_counted_as_stuck(env, tmp_path):
    # Models never see a captured value, so they sometimes extract it again "to check".
    script = yaml.safe_load((SCRIPTED_DISCOVERY_DIR / "get_savings_balance.yaml").read_text(encoding="utf-8"))
    extract = next(s for s in script["steps"] if s["tool"] == "extract")
    done = script["steps"].pop()
    script["steps"] += [extract, extract, done]  # three reads of the same value, then done
    path = tmp_path / "rereads.yaml"
    path.write_text(yaml.safe_dump(script), encoding="utf-8")
    profile, policy = env
    log = capability_workflows.new_run_log("discover", profile, quiet=True)

    outcome = asyncio.run(capability_workflows.learn_capability(
        "What's the savings balance for member 12345?", profile, log, ScriptedClient(path),
        policy=policy, store=CapabilityStore(tmp_path / "capabilities")))

    assert outcome.result.status == "done", outcome.result.reason
    assert [s.action for s in outcome.result.trace].count("extract") == 1
    assert outcome.ok and outcome.validation.outputs == {"savings_balance": "1204.50"}


def learn_balance(env, tmp_path):
    profile, policy = env
    store = CapabilityStore(tmp_path / "capabilities")
    log = capability_workflows.new_run_log("discover", profile, quiet=True)
    script = ScriptedClient(SCRIPTED_DISCOVERY_DIR / "get_savings_balance.yaml")
    outcome = asyncio.run(capability_workflows.learn_capability(
        "What's the savings balance for member 12345?", profile, log, script, policy=policy,
        store=store))
    return outcome, store


def test_a_learned_capability_must_pass_three_validation_replays(env, tmp_path):
    outcome, store = learn_balance(env, tmp_path)
    assert outcome.ok and len(outcome.validations) == 3
    saved = store.load("get_savings_balance")
    assert saved.status == "draft" and len(saved.provenance.validation_runs) == 3


def test_a_flaky_capability_is_not_saved(env, tmp_path, monkeypatch):
    real = capability_workflows.run_capability
    calls = []

    async def second_one_fails(cap, params, *args, **kwargs):
        calls.append(1)
        result = await real(cap, params, *args, **kwargs)
        return result.model_copy(update={"status": "failed"}) if len(calls) == 2 else result

    monkeypatch.setattr(capability_workflows, "run_capability", second_one_fails)
    outcome, _ = learn_balance(env, tmp_path)
    assert not outcome.saved_to and "validation replay 2 of 3" in outcome.message
    assert len(calls) == 2 and not list((tmp_path / "capabilities").rglob("*.yaml"))
