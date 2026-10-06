"""Request routing without the AI: reuse, discover or ask, never a wrong match."""

from ui_automation.catalog.request_router import _keyword_route

from .capability_examples import SAVINGS


def test_reuses_a_capability_when_every_word_is_covered():
    r = _keyword_route("What's the savings balance for member 23456?", [SAVINGS])
    assert (r.kind, r.capability.name, r.params) == ("replay", "get_savings_balance", {"member_number": "23456"})


def test_never_answers_a_checking_question_with_the_savings_capability():
    r = _keyword_route("Read the checking account available balance for member 12345", [SAVINGS])
    assert r.kind == "discover"


def test_asks_for_a_missing_input():
    r = _keyword_route("What's the savings balance?", [SAVINGS])
    assert r.kind == "clarify" and "member_number" in r.note


def test_unrelated_request_is_not_forced_onto_a_capability():
    assert _keyword_route("transfer 500 to member 12345", [SAVINGS]).kind == "discover"
