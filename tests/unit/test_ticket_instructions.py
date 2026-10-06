"""Ticket instructions say what to check, what to click, and how to refuse."""

from ui_automation.human_handoff.ticket_instructions import approval, problem, stuck

from .capability_examples import SAVINGS


def test_approval_instructions_say_what_to_check_and_click():
    steps = approval(SAVINGS, SAVINGS.steps[0], {"member_number": "23456", "initial_deposit": "100.00"}, "Confirm")
    text = " ".join(steps)
    assert "member number 23456" in text and "initial deposit 100.00" in text
    assert "click 'Approve'" in text and "clicks 'Confirm'" in text and "Don't do it" in text


def test_stuck_instructions_are_plain_words():
    step = SAVINGS.steps[0]
    why = problem("TARGET_NOT_FOUND", step)
    text = " ".join(stuck(step, {"member_number": "23456"}, why, "'Member Search'"))
    assert why.startswith("It couldn't find ") and "'Member Search'" in text
    assert "TARGET_NOT_FOUND" not in text and "label_anchor" not in text and "role_name" not in text
