"""The seam between the system and any LLM provider."""

from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

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
