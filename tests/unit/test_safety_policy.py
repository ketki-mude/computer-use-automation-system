"""Safety policy: the allowlist, risk classes, and JavaScript navigation."""

import pytest

from ui_automation.safety.safety_policy import SafetyPolicy


@pytest.fixture
def policy() -> SafetyPolicy:
    return SafetyPolicy.load()


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


def test_allowlist_resolves_dot_segments_and_encoding(policy):
    assert not policy.allows_url("http://127.0.0.1:8000/app/../__admin/reset")[0]
    assert not policy.allows_url("http://127.0.0.1:8000/app/%2e%2e/__admin/reset")[0]
    assert policy.allows_url("http://127.0.0.1:8000/app/./search")[0]
