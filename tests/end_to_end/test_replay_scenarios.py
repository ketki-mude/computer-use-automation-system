"""End-to-end replay against the real mock bank in a real (headless) browser, no LLM.

Covers every branch of the result contract, every recovery, the commit-point rules, and
tickets resolved by a simulated operator who acts on the same live session. The mock bank
runs in this process on its own port, so faults and the bank's audit can be set and read
directly. Run only the fast tests with: pytest -m "not integration"
"""

import asyncio

import pytest

from mock_bank import bank_app as bank
from mock_bank import fault_injection
from ui_automation import capability_workflows
from ui_automation.catalog.capability_store import CapabilityStore
from ui_automation.human_handoff.tickets import LocalTicketInbox, TicketInbox
from ui_automation.screen.screen_interface import NotInControl

pytestmark = pytest.mark.integration
OPEN = {"member_number": "23456", "share_type": "REGULAR SAVINGS", "initial_deposit": "100.00"}


def replay(env, name, params, **kw):
    profile, policy = env
    log = capability_workflows.new_run_log("test", profile, quiet=True)
    return asyncio.run(capability_workflows.run_capability(CapabilityStore().load(name), params, profile, log, policy=policy, **kw))


def opened() -> int:
    return len(bank.AUDIT["opened"])


async def with_operator(env, name, params, act, **kw):
    """Run a replay while `act(ticket, inbox, controller, screen)` plays the operator."""
    profile, policy = env
    inbox = TicketInbox()
    live = {}

    async def on_ready(screen, controller):
        live["screen"], live["controller"] = screen, controller

    async def operator():
        while not [t for t in inbox.tickets.values() if t.state == "open"]:
            await asyncio.sleep(0.1)
        ticket = next(t for t in inbox.tickets.values() if t.state == "open")
        await act(ticket, inbox, live["controller"], live["screen"])
        return ticket

    log = capability_workflows.new_run_log("test", profile, quiet=True)
    run = capability_workflows.run_capability(CapabilityStore().load(name), params, profile, log, policy=policy,
                          inbox=LocalTicketInbox(inbox), on_ready=on_ready, ticket_timeout_s=20, **kw)
    return await asyncio.gather(run, operator())


async def take(ticket, inbox, controller):
    with pytest.raises(NotInControl):  # nobody may act before taking the ticket
        await controller.human_click(5, 5)
    inbox.act(ticket.id, "take", operator="tester")
    for _ in range(30):  # the run notices within a poll interval
        if controller.human_in_control:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the run never handed control to the operator")


async def click_center(controller, screen, frame, role, name):
    box = await screen.frame(frame).get_by_role(role, name=name).bounding_box()
    before = len(controller.open.actions)
    await controller.human_click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(50):  # wait until the click is recorded on the ticket
        if len(controller.open.actions) > before:
            break
        await asyncio.sleep(0.1)
    await screen.settle()


# ---------------------------------------------------------------- results and business outcomes

def test_success(env):
    r = replay(env, "get_savings_balance", {"member_number": "12345"})
    assert (r.status, r.outputs) == ("success", {"savings_balance": "1204.50"})


def test_generalizes_to_another_member(env):
    r = replay(env, "get_savings_balance", {"member_number": "23456"})  # 123456 also matches the search
    assert r.outputs == {"savings_balance": "15002.75"}


@pytest.mark.parametrize("member,code", [("99999", "RECORD_NOT_FOUND"), ("66666", "PERMISSION_DENIED")])
def test_business_outcomes(env, member, code):
    r = replay(env, "get_savings_balance", {"member_number": member})
    assert (r.status, r.outcome.code) == ("business_outcome", code)


def test_invalid_input_is_rejected_before_acting(env):
    r = replay(env, "get_savings_balance", {"member_number": "12a45"})
    assert (r.status, r.error.kind) == ("failed", "INPUT_INVALID")


def test_validation_error_is_a_business_answer(env):
    r = replay(env, "open_sub_account", {**OPEN, "share_type": "MONEY MARKET"}, allow_irreversible=True)
    assert (r.status, r.outcome.code, opened()) == ("business_outcome", "VALIDATION_ERROR", 0)


# ---------------------------------------------------------------- recoveries

@pytest.mark.parametrize("fault,condition", [
    ({"maintenance": 1}, "MAINTENANCE_NOTICE"),
    ({"expire_after": 2}, "SESSION_EXPIRED"),
    ({"popup": 1}, "PASSWORD_EXPIRY_NOTICE"),
    ({"unavailable": 1}, "SERVICE_UNAVAILABLE"),
])
def test_recovers_by_itself(env, fault, condition):
    fault_injection.FAULTS.update(fault)
    r = replay(env, "get_savings_balance", {"member_number": "12345"})
    assert r.status == "success" and r.outputs == {"savings_balance": "1204.50"}
    assert condition in [x.condition for x in r.recoveries]


def test_a_new_app_version_replays_on_fallback_locators_and_reports_drift(env):
    fault_injection.FAULTS.update({"variant": 1})  # 7.5: "Member No." label, "Find" button
    r = replay(env, "get_savings_balance", {"member_number": "12345"})
    assert r.status == "success" and r.outputs == {"savings_balance": "1204.50"}
    assert {d.step_id for d in r.drift} == {"sign-on", "enter_member_number", "click_search"}
    assert "7.5.0" in next(d.note for d in r.drift if d.step_id == "sign-on")


def test_slow_page_is_waited_for(env):
    r = replay(env, "get_savings_balance", {"member_number": "50000"})  # 4 s server delay
    assert r.outputs == {"savings_balance": "42.00"}


# ---------------------------------------------------------------- hard failures

def test_server_error_stops_with_evidence(env):
    r = replay(env, "get_savings_balance", {"member_number": "70000"})
    assert (r.status, r.error.kind, r.error.step_id) == ("failed", "APP_ERROR", "click_view")
    assert r.error.screenshot and r.error.dom_snapshot


def test_unexpected_question_is_cancelled_and_stops(env):
    fault_injection.FAULTS.update({"confirm_popup": 1})
    r = replay(env, "get_savings_balance", {"member_number": "12345"})
    assert (r.status, r.error.kind) == ("failed", "UNKNOWN_STATE")
    assert "cancelled for safety" in r.error.observed


# ---------------------------------------------------------------- the irreversible class

def test_irreversible_refused_without_a_person_or_approval(env):
    r = replay(env, "open_sub_account", OPEN)
    assert (r.status, r.error.kind, opened()) == ("failed", "APPROVAL_REQUIRED", 0)


def test_irreversible_with_explicit_approval(env):
    r = replay(env, "open_sub_account", OPEN, allow_irreversible=True)
    assert r.status == "success" and r.outputs["confirmation_number"] and opened() == 1


def test_session_expiry_after_the_commit_is_never_retried(env):
    fault_injection.FAULTS.update({"expire_after": 7})  # the 7th main-frame request is the Confirm
    r = replay(env, "open_sub_account", OPEN, allow_irreversible=True)
    assert (r.status, r.error.kind) == ("failed", "ESCALATION_UNRESOLVED")
    assert "irreversible" in r.error.observed and not r.recoveries  # no restart happened


# ---------------------------------------------------------------- tickets: a person on the live session

def test_approval_ticket_person_confirms(env):
    async def act(ticket, inbox, controller, screen):
        assert ticket.kind == "approval" and "click 'Confirm'" in " ".join(ticket.instructions)
        await take(ticket, inbox, controller)
        await click_center(controller, screen, "main", "button", "Confirm")
        inbox.act(ticket.id, "resume")

    r, ticket = asyncio.run(with_operator(env, "open_sub_account", OPEN, act))
    assert r.status == "success" and r.outputs["confirmation_number"] and opened() == 1
    assert r.handoffs[0].resolution == "resumed" and r.handoffs[0].operator == "tester"
    assert {"action": "click", "name": "Confirm"}.items() <= ticket.human_actions[0].items()


def test_approval_ticket_person_rejects(env):
    async def act(ticket, inbox, controller, screen):
        await take(ticket, inbox, controller)
        inbox.act(ticket.id, "reject")

    r, _ = asyncio.run(with_operator(env, "open_sub_account", OPEN, act))
    assert (r.status, r.outcome.code, opened()) == ("business_outcome", "REJECTED_BY_OPERATOR", 0)


def test_resuming_without_doing_the_risky_step_submits_nothing(env):
    async def act(ticket, inbox, controller, screen):
        await take(ticket, inbox, controller)
        inbox.act(ticket.id, "resume")  # "I did it" without clicking Confirm

    r, _ = asyncio.run(with_operator(env, "open_sub_account", OPEN, act))
    assert (r.status, r.error.kind, opened()) == ("failed", "ESCALATION_UNRESOLVED", 0)
    assert "nothing was submitted" in r.error.observed and len(r.handoffs) == 1  # no second ticket


def test_identity_check_person_types_the_code(env):
    fault_injection.FAULTS.update({"verify_identity": 1})

    async def act(ticket, inbox, controller, screen):
        assert ticket.kind == "needs_human" and ticket.context["session"] in " ".join(ticket.instructions)
        await take(ticket, inbox, controller)
        code = bank.pending_codes()[ticket.context["session"]]
        box = await screen.page.locator("input[name='ctl00$cph1$txtOtp']").bounding_box()
        await controller.human_click(box["x"] + 10, box["y"] + box["height"] / 2)
        await controller.human_type(code)
        await click_center(controller, screen, None, "button", "Verify")
        inbox.act(ticket.id, "resume")

    r, ticket = asyncio.run(with_operator(env, "get_savings_balance", {"member_number": "12345"}, act))
    assert r.status == "success" and r.handoffs[0].resolution == "resumed"
    typed = [a for a in ticket.human_actions if a["action"] == "fill"]
    assert typed and set(typed[0]["value"]) == {"•"}  # the code itself is never recorded


def test_stuck_ticket_then_resume(env):
    fault_injection.FAULTS.update({"confirm_popup": 1})

    async def act(ticket, inbox, controller, screen):
        assert ticket.kind == "stuck" and "Print a receipt" in " ".join(ticket.instructions)
        await take(ticket, inbox, controller)
        inbox.act(ticket.id, "resume")  # nothing to fix on screen: the dialog was already cancelled

    r, _ = asyncio.run(with_operator(env, "get_savings_balance", {"member_number": "12345"}, act))
    assert r.status == "success" and r.handoffs[0].resolution == "resumed"


def test_aborted_ticket_fails_cleanly(env):
    async def act(ticket, inbox, controller, screen):
        inbox.act(ticket.id, "abort")

    r, _ = asyncio.run(with_operator(env, "open_sub_account", OPEN, act))
    assert (r.status, r.error.kind, opened()) == ("failed", "ESCALATION_UNRESOLVED", 0)


# ---------------------------------------------------------------- staff stepping in

async def with_staff_request(env, name, params, request, act=None):
    """Staff asks to take over or stop as soon as the run starts; `act` plays the operator."""
    profile, policy = env
    inbox = TicketInbox()

    async def on_ready(screen, controller):
        controller.ask_for(request, "tester")  # honoured at the first step boundary

    async def operator():
        if act is None:
            return None
        while not [t for t in inbox.tickets.values() if t.state == "open"]:
            await asyncio.sleep(0.1)
        ticket = next(t for t in inbox.tickets.values() if t.state == "open")
        await act(ticket, inbox)
        return ticket

    log = capability_workflows.new_run_log("test", profile, quiet=True)
    run = capability_workflows.run_capability(
        CapabilityStore().load(name), params, profile, log, policy=policy,
        inbox=LocalTicketInbox(inbox), on_ready=on_ready, ticket_timeout_s=20)
    return await asyncio.gather(run, operator())


def test_staff_takeover_pauses_between_steps_and_resumes(env):
    async def act(ticket, inbox):
        assert ticket.kind == "takeover" and "tester asked to step in" in " ".join(ticket.instructions)
        inbox.act(ticket.id, "take", operator="tester")
        inbox.act(ticket.id, "resume")

    r, ticket = asyncio.run(with_staff_request(env, "get_savings_balance", {"member_number": "12345"},
                                               "takeover", act))
    assert r.status == "success" and r.outputs == {"savings_balance": "1204.50"}
    assert r.handoffs[0].resolution == "resumed" and ticket.step_id == "click_member_search"


def test_staff_stop_ends_the_run_cleanly(env):
    r, _ = asyncio.run(with_staff_request(env, "open_sub_account", OPEN, "stop"))
    assert (r.status, r.error.kind, opened()) == ("failed", "STOPPED_BY_OPERATOR", 0)
    assert "nothing irreversible had run" in r.error.observed
