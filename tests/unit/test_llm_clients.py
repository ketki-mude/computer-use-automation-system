"""Choosing the model provider, and the schema shape sent to it."""

from pydantic import BaseModel

from ui_automation.discovery import llm_clients


class Inner(BaseModel):
    name: str


class Outer(BaseModel):
    items: list[Inner]


def test_schema_references_are_written_out_in_place():
    schema = llm_clients.inline_refs(Outer.model_json_schema())
    assert "$defs" not in str(schema) and "$ref" not in str(schema)
    assert schema["properties"]["items"]["items"]["properties"]["name"]["type"] == "string"


def test_openai_is_used_when_its_key_is_set(monkeypatch):
    monkeypatch.setattr(llm_clients, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(llm_clients, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm_clients, "GEMINI_API_KEY", "g-test")
    assert llm_clients.active_provider() == "openai"
    assert isinstance(llm_clients.configured_client(), llm_clients.OpenAIClient)
    monkeypatch.setattr(llm_clients, "OPENAI_API_KEY", "")
    assert llm_clients.active_provider() == "gemini"
    monkeypatch.setattr(llm_clients, "GEMINI_API_KEY", "")
    assert llm_clients.active_provider() is None and llm_clients.configured_client() is None
