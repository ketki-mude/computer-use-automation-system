"""Ticket inbox: lifecycle, allowed buttons per kind, abort and timeout."""

import pytest

from ui_automation.human_handoff.tickets import NewTicket, TicketInbox


def ticket(kind="needs_human") -> NewTicket:
    return NewTicket(run_id="r1", capability="c@1.0.0", step_id="s1", kind=kind, title="t",
                     reason="why", instructions=["do this"])


def test_ticket_lifecycle():
    inbox = TicketInbox()
    t = inbox.create(ticket())
    assert t.state == "open"
    inbox.act(t.id, "take", operator="ketki")
    assert (t.state, t.operator) == ("in_progress", "ketki")
    inbox.act(t.id, "resume")
    assert t.state == "resumed"


@pytest.mark.parametrize("action", ["resume", "done", "approve", "reject"])
def test_cannot_resolve_a_ticket_nobody_took(action):
    inbox = TicketInbox()
    t = inbox.create(ticket("approval"))
    with pytest.raises(ValueError):
        inbox.act(t.id, action)


def test_buttons_depend_on_ticket_kind():
    inbox = TicketInbox()
    approval_t = inbox.create(ticket("approval"))
    stuck_t = inbox.create(ticket("stuck"))
    for t in (approval_t, stuck_t):
        inbox.act(t.id, "take", operator="a")
    assert [a for a, _ in inbox.view(approval_t)["buttons"]] == ["approve", "reject"]
    with pytest.raises(ValueError, match="not available"):
        inbox.act(stuck_t.id, "reject")  # only approvals can be rejected
    inbox.act(approval_t.id, "reject")
    assert approval_t.state == "rejected"


def test_abort_and_timeout_from_open():
    inbox = TicketInbox()
    a, b = inbox.create(ticket()), inbox.create(ticket())
    inbox.act(a.id, "abort")
    inbox.act(b.id, "timed_out")
    assert (a.state, b.state) == ("aborted", "timed_out")
