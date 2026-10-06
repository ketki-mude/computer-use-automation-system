"""An AI agent lists the tools and invokes one by name over HTTP; replay runs with no LLM."""

import asyncio

import httpx
import pytest

from ui_automation.web.api_routes import create_app
from ui_automation.web.control_room import ControlRoom

pytestmark = pytest.mark.integration


async def as_agent(bank_url: str, name: str, inputs: dict) -> tuple[list[dict], httpx.Response]:
    app = create_app(ControlRoom(bank_url))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui") as c:
        tools = (await c.get("/api/tools")).json()["tools"]
        response = await c.post(f"/api/capabilities/{name}/invoke", json={"inputs": inputs})
    return tools, response


def test_agent_lists_tools_and_invokes_one(env):
    profile, _ = env
    tools, r = asyncio.run(as_agent(profile.base_url, "get_savings_balance", {"member_number": "23456"}))
    assert {"get_savings_balance", "open_sub_account"} <= {t["name"] for t in tools}
    result = r.json()
    assert r.status_code == 200 and result["status"] == "success"
    assert result["outputs"] == {"savings_balance": "15002.75"}


def test_bad_inputs_get_the_same_result_contract(env):
    profile, _ = env
    _, r = asyncio.run(as_agent(profile.base_url, "get_savings_balance", {"member_number": "12a45"}))
    assert (r.json()["status"], r.json()["error"]["kind"]) == ("failed", "INPUT_INVALID")
    _, r = asyncio.run(as_agent(profile.base_url, "no_such_capability", {}))
    assert r.status_code == 404
