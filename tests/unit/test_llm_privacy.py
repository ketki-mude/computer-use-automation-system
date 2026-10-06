"""What a model may see: requests and screens with member data replaced by placeholders."""

import asyncio

import pytest

from ui_automation.catalog.request_router import route
from ui_automation.discovery.llm_clients import ToolCall
from ui_automation.discovery.llm_privacy import (
    PlaceholderError,
    ScreenMasker,
    fill_placeholders,
    mask_request,
    readable_part,
)
from ui_automation.safety.log_masking import Redactor

from .capability_examples import SAVINGS

APP_PATTERNS = {"ACCOUNT": r"\b\d{5,10}-S\d{4}\b", "MONEY": r"\(?-?\$[\d,]+\.\d{2}\)?"}
MEMBER_SCREEN = """## frame "banner" · title "" · /app/banner
| [1]AcmeCore Teller 7.4.2 build 1187 | [2]Example Community Credit Union | User: teller01 [3] link "Sign Off" |
## frame "main" · title "Member Inquiry - 23456" · /app/member
| [11]Member Name: | [12]ROBERT DEMO | [13]Member Since: | [14]01/09/2015 |
| [19]SSN: | [20]***-**-5566 | [21]Date of Birth: | [22]**/**/1988 |
| [32]0000 | [33]REGULAR SAVINGS | [34]23456-S0000 | [35]$15,002.75 | [36]$15,002.75 | [37]OPEN |
| [21]123456 | [22]JOHN R EXAMPLE | [25] link "View" |
[46] button "Open Sub-Account" [47] button "Close Membership\""""
SENSITIVE = ["23456", "15,002.75", "ROBERT DEMO", "teller01", "123456", "JOHN R EXAMPLE",
             "2015", "1988", "5566", "1187"]


def test_request_values_become_placeholders():
    r = mask_request("Open a REGULAR SAVINGS sub-account for member 23456 with a deposit of $1,100.00.")
    assert r.text == ("Open a REGULAR SAVINGS sub-account for member {value_1} with a deposit "
                      "of {value_2}.")
    assert r.values == {"value_1": "23456", "value_2": "1100.00"}
    assert r.resolve("{value_1}") == "23456" and r.resolve("value_2") == "1100.00"
    assert r.resolve("REGULAR SAVINGS") == "REGULAR SAVINGS"  # visible words stay as written
    assert r.named({"value_1": "member_number"}).startswith("Open a REGULAR SAVINGS sub-account for member {member_number}")
    with pytest.raises(PlaceholderError):
        r.resolve("{value_9}")


def test_screen_is_masked_but_still_navigable():
    redactor = Redactor()
    redactor.register("teller01", "secret")
    redactor.register("ROBERT DEMO", "pii")
    redactor.register("JOHN R EXAMPLE", "pii")
    seen = ScreenMasker({"member_number": "23456"}, redactor, APP_PATTERNS).text(MEMBER_SCREEN)
    for value in SENSITIVE:
        assert value not in seen, value
    assert 'title "Member Inquiry - {member_number}"' in seen  # the member's own row and title
    assert "| [33]REGULAR SAVINGS | [34]<ACCOUNT> | [35]<MONEY> | [36]<MONEY> |" in seen
    assert "[12]<VALUE>" in seen and "User: <SECRET>" in seen and "[14]<DATE>" in seen
    assert "[21]<NUMBER> | [22]<VALUE>" in seen  # another member's row: nothing to copy
    assert 'button "Close Membership"' in seen and "7.4.2" in seen  # controls stay readable


def test_typed_placeholders_become_values_in_code_only():
    inputs = {"member_number": "23456", "initial_deposit": "100.00"}
    assert fill_placeholders("{member_number}", inputs) == "23456"
    assert fill_placeholders("plain words", inputs) == "plain words"
    with pytest.raises(PlaceholderError, match="unknown placeholder"):
        fill_placeholders("{ssn}", inputs)
    assert readable_part("<ACCOUNT> SHARE DRAFT CHECKING (<MONEY> avail)") == "SHARE DRAFT CHECKING"
    assert readable_part("REGULAR SAVINGS") == "REGULAR SAVINGS"


class RecordingRouterLLM:
    """Answers like a model would, and remembers exactly what it was shown."""

    def __init__(self):
        self.prompts: list[str] = []

    async def call_tool(self, system, prompt, tools, image=None):
        self.prompts.append(system + prompt + repr(tools))
        return ToolCall("get_savings_balance", {"member_number": "{value_1}"}, "fake")


def test_router_sees_placeholders_and_code_fills_in_the_value():
    llm = RecordingRouterLLM()
    r = asyncio.run(route("What's the savings balance for member 23456?", [SAVINGS], llm))
    assert (r.kind, r.how, r.params) == ("replay", "llm", {"member_number": "23456"})
    assert "23456" not in llm.prompts[0] and "{value_1}" in llm.prompts[0]
