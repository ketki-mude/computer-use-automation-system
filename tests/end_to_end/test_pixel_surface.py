"""Replay with no DOM: the same capability, read only through screenshots (OCR) and driven
only by mouse and keys at coordinates computed at click time."""

import asyncio

import pytest

pytest.importorskip("rapidocr", reason='the pixel surface needs: pip install -e ".[pixel]"')

from ui_automation import capability_workflows
from ui_automation.catalog.capability_store import CapabilityStore
from ui_automation.screen.pixel_screen import PixelSurface

pytestmark = pytest.mark.integration


def pixel_replay(env, member: str, surface: PixelSurface | None = None):
    profile, policy = env
    log = capability_workflows.new_run_log("pixel", profile, quiet=True)
    return asyncio.run(capability_workflows.run_capability(
        CapabilityStore().load("get_savings_balance"), {"member_number": member}, profile, log,
        policy=policy, pixel=surface or PixelSurface()))


def test_replays_with_no_dom(env):
    r = pixel_replay(env, "12345")
    assert (r.status, r.outputs) == ("success", {"savings_balance": "1204.50"}), r.error


def clicks(result) -> list[tuple[int, int]]:
    """Where the run clicked or typed, from its event log (window points)."""
    import json
    from pathlib import Path

    events = Path(result.evidence_dir) / "events.jsonl"
    return [(e["x"], e["y"]) for e in map(json.loads, events.read_text(encoding="utf-8").splitlines())
            if e["type"] == "pixel_action"]


def test_another_window_size_and_display_scale(env):
    # Coordinates are computed from the screen at click time, never stored, so a smaller
    # window on a 150% display replays the same capability, with different click points.
    standard = pixel_replay(env, "23456")
    small = pixel_replay(env, "23456", PixelSurface(window=(1100, 760), scale=1.5))
    for r in (standard, small):
        assert (r.status, r.outputs) == ("success", {"savings_balance": "15002.75"}), r.error
    assert clicks(standard) != clicks(small)


def test_a_business_outcome_on_pixels(env):
    r = pixel_replay(env, "99999")
    assert (r.status, r.outcome.code) == ("business_outcome", "RECORD_NOT_FOUND")
