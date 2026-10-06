"""The seam between the system and any LLM provider.

LLMClient is the interface the rest of the system uses. GeminiClient implements it with the
google-genai SDK (retries overloaded and rate-limited calls, then falls back to the next
model). ScriptedClient answers from a YAML file instead of a model: for tests, offline demos,
and reviewers without an API key. Nothing outside this file knows which provider is used.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

import httpx
import yaml
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from ..evidence_writer import RunLog
from ..settings import DISCOVERY_MODEL, FALLBACK_MODELS, GEMINI_API_KEY, LLM_REQUEST_TIMEOUT_MS

T = TypeVar("T", bound=BaseModel)

class LLMError(Exception):
    pass

@dataclass
class ToolCall:
    name: str
    args: dict
    model: str  # the model that actually answered (after any fallback)

class LLMClient(Protocol):
    async def call_tool(self, system: str, prompt: str, tools: list[dict],
                        image: bytes | None = None) -> ToolCall:
        """Ask for exactly one tool call from `tools` (JSON-schema function declarations)."""
        ...

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        """Ask for a JSON answer that validates against a Pydantic model."""
        ...

RETRYABLE = {429, 500, 503, 504}

def _retry_delay(e: errors.APIError) -> float | None:
    m = re.search(r"retryDelay['\"]?:\s*['\"](\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) if m else None

class GeminiClient:
    def __init__(self, api_key: str, models: list[str], log: RunLog | None = None):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (see .env.example)")
        # A finite timeout: a connection that died (network change, laptop sleep) must
        # fail and be retried instead of hanging the run.
        self._client = genai.Client(api_key=api_key,
                                    http_options=types.HttpOptions(timeout=LLM_REQUEST_TIMEOUT_MS))
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
        # ensure_ascii=False: redaction runs on this text later, and must see "José", not "Jos\u00e9".
        self.log.defer_text("llm.jsonl", json.dumps(entry, default=str, ensure_ascii=False) + "\n")

CONTROL = re.compile(r'\[(\d+)\] (\w+)(?: "([^"]*)")?(?: \(label: "([^"]*)"\))?')

REF_TEXT = re.compile(r"^\[(\d+)\](.*)$")

def _segments(line: str) -> list[tuple[int | None, str]]:
    """Cells of a table-row line "| [3]text | [4] link "View" |" as (ref, text)."""
    out = []
    for seg in line.strip().strip("|").split("|"):
        seg = seg.strip()
        m = REF_TEXT.match(seg)
        out.append((int(m.group(1)), m.group(2).strip()) if m else (None, seg))
    return out

def find_ref(screen: str, role: str | None = None, name: str | None = None,
             label: str | None = None, row: str | None = None, column: str | None = None,
             next_to: str | None = None) -> int:
    """Find an element number on the screen text, the way a person would point at it:
    a control by role/name/label (optionally inside the table row containing `row`),
    the cell under `column` in the row containing `row`, or the cell `next_to` a label."""
    lines = [ln for ln in screen.splitlines() if ln.startswith("|")]
    if next_to is not None:
        for line in lines:
            segs = _segments(line)
            for i, (_, text) in enumerate(segs[:-1]):
                if text == next_to and segs[i + 1][0] is not None:
                    return segs[i + 1][0]
    elif column is not None:
        header = next((ln for ln in lines if column in [t for _, t in _segments(ln)]), None)
        if header:
            idx = [t for _, t in _segments(header)].index(column)
            for line in lines:
                segs = _segments(line)
                if any(t == row for _, t in segs) and idx < len(segs) and segs[idx][0] is not None:
                    return segs[idx][0]
    else:
        scope = [ln for ln in lines if any(t == row for _, t in _segments(ln))] if row else \
            screen.splitlines()
        for line in scope:
            for m in CONTROL.finditer(line):
                ref, r, n, lab = m.groups()
                if (role and r != role) or (name is not None and n != name) or \
                        (label is not None and lab != label):
                    continue
                return int(ref)
    raise LLMError(f"scripted target not on screen: role={role} name={name} label={label} "
                   f"row={row} column={column} next_to={next_to}")

class ScriptedClient:
    def __init__(self, script_path: Path):
        script = yaml.safe_load(Path(script_path).read_text(encoding="utf-8"))
        self.goal_spec = script["goal_spec"]
        self.steps = list(script["steps"])

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        """The script's goal spec. An input written with `after: member` takes the placeholder
        that follows that word in the (masked) goal, so one script serves any member."""
        spec = {**self.goal_spec, "inputs": []}
        for inp in self.goal_spec.get("inputs", []):
            inp = dict(inp)
            if after := inp.pop("after", None):
                m = re.search(rf"{re.escape(after)}\D*?(\{{value_\d+\}})", prompt, re.IGNORECASE)
                if m is None:
                    raise LLMError(f"the request gives no value after {after!r}")
                inp["value"] = m.group(1)
            spec["inputs"].append(inp)
        return schema.model_validate(spec)

    async def call_tool(self, system: str, prompt: str, tools: list[dict],
                        image: bytes | None = None) -> ToolCall:
        if not self.steps:
            raise LLMError("the script has no more steps")
        step = self.steps.pop(0)
        args = dict(step.get("args") or {})
        if "target" in step:
            screen = prompt.split("CURRENT SCREEN:", 1)[-1]
            args["ref"] = find_ref(screen, **step["target"])
        args.setdefault("reason", step.get("reason", "scripted step"))
        return ToolCall(step["tool"], args, "scripted")


def llm_configured() -> bool:
    """Is a model available for discovery and routing (an API key is set)?"""
    return bool(GEMINI_API_KEY)


def configured_client(log: RunLog | None = None) -> LLMClient | None:
    """The model client from settings, or None when no API key is configured. Callers depend
    on the LLMClient protocol only; which vendor sits behind it is decided here."""
    if not llm_configured():
        return None
    return GeminiClient(GEMINI_API_KEY, [DISCOVERY_MODEL, *FALLBACK_MODELS], log)
