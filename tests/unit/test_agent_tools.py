"""The agent-facing contract: tool definitions and the published JSON Schemas."""

from ui_automation.catalog.agent_tools import SCHEMAS_DIR, schema_documents, tool_catalog

from .capability_examples import SAVINGS, make_capability


def test_only_approved_capabilities_are_offered_as_tools():
    tools = tool_catalog([SAVINGS, make_capability(name="draft_one")])  # make_capability is a draft
    assert [t["name"] for t in tools] == ["get_savings_balance"]
    tool = tools[0]
    assert tool["parameters"]["required"] == ["member_number"]
    assert tool["outputs"]["savings_balance"]["type"] == "decimal"


def test_published_schemas_match_the_models():
    # Regenerate with `ui-automation schemas` after changing a model.
    for name, text in schema_documents().items():
        assert (SCHEMAS_DIR / name).read_text(encoding="utf-8") == text, f"{name} is out of date"
