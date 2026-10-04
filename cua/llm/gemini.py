"""Gemini implementation of LLMClient (google-genai SDK).

Retries overloaded (503) and rate-limited (429) calls with backoff, then falls back
to the next model in the list. Every call is appended to the run's llm.jsonl, so the
evidence shows exactly what the model was asked and what it answered.
"""

import asyncio
import json
import re

import httpx
from google import genai
from google.genai import errors, types

from ..evidence import RunLog
from .base import LLMError, T, ToolCall

RETRYABLE = {429, 500, 503, 504}
REQUEST_TIMEOUT_MS = 60_000


class GeminiClient:
    def __init__(self, api_key: str, models: list[str], log: RunLog | None = None):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (see .env.example)")
        # A finite timeout: a connection that died (network change, laptop sleep) must
        # fail and be retried instead of hanging the run.
        self._client = genai.Client(api_key=api_key,
                                    http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))
        self.models = models
        self.log = log
        self._exhausted: set[str] = set()  # models whose quota ran out during this run

    async def call_tool(self, system: str, prompt: str, tools: list[dict],
                        image: bytes | None = None) -> ToolCall:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["parameters"]) for t in tools])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        parts = [types.Part.from_text(text=prompt)]
        if image:
            parts.append(types.Part.from_bytes(data=image, mime_type="image/png"))
        response, model = await self._generate([types.Content(role="user", parts=parts)], config)
        calls = response.function_calls or []
        if not calls:
            raise LLMError(f"{model} answered without a tool call: {(response.text or '')[:200]!r}")
        call = ToolCall(calls[0].name, dict(calls[0].args or {}), model)
        self._record(prompt, {"tool": call.name, "args": call.args}, model)
        return call

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response, model = await self._generate(prompt, config)
        result = schema.model_validate_json(response.text)
        self._record(prompt, result.model_dump(), model)
        return result

    async def _generate(self, contents, config):
        last = None
        for model in [m for m in self.models if m not in self._exhausted]:
            for attempt in range(3):
                try:
                    resp = await self._client.aio.models.generate_content(
                        model=model, contents=contents, config=config)
                    return resp, model
                except errors.APIError as e:
                    if e.code not in RETRYABLE:
                        raise LLMError(f"{model}: {e.code} {e.message}") from e
                    last, why, delay = e, f"returned {e.code}", _retry_delay(e) or 2 * (attempt + 1)
                except httpx.TransportError as e:  # dropped connection, DNS, timeout
                    last, why, delay = e, f"network error ({type(e).__name__})", 2 * (attempt + 1)
                if delay > 60:  # a daily quota, not a blip: stop using this model for the run
                    self._exhausted.add(model)
                    if self.log:
                        self.log.event("llm_retry", f"{model} quota exhausted; switching model", model=model)
                    break
                if self.log:
                    self.log.event("llm_retry", f"{model} {why}; retrying in {delay:.0f}s", model=model)
                await asyncio.sleep(delay)
        raise LLMError(f"all models failed; last error: {last}")

    def _record(self, prompt: str, response: dict, model: str) -> None:
        if not self.log:
            return
        entry = {"model": model, "prompt": prompt, "response": response}
        self.log.defer_text("llm.jsonl", json.dumps(entry, default=str) + "\n")


def _retry_delay(e: errors.APIError) -> float | None:
    m = re.search(r"retryDelay['\"]?:\s*['\"](\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) if m else None
