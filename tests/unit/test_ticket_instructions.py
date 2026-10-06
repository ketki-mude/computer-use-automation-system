"""Ticket instructions say what to check, what to click, and how to refuse."""

from ui_automation.human_handoff.ticket_instructions import approval

from .capability_examples import SAVINGS


def test_approval_instructions_say_what_to_check_and_click():
    steps = approval(SAVINGS, SAVINGS.steps[0], {"member_number": "23456", "initial_deposit": "100.00"}, "Confirm")
    text = " ".join(steps)
    assert "member number 23456" in text and "initial deposit 100.00" in text
    assert "click 'Confirm'" in text and "Don't do it" in text
