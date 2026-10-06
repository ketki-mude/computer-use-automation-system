"""Turn a free-text request into "replay this capability with these inputs", or "nothing
fits: discover it", or "ask for a missing input".

The LLM chooses from the catalog through tool calling: each approved capability is offered
as a function (name, description, typed inputs), plus no_matching_capability and
ask_clarification. It can only choose from that list; its arguments are then checked
against the capability's schema. The model sees the request with its values hidden
({value_1}); it passes placeholders back and code swaps in the real values. If the LLM is
unavailable, a keyword match over names and descriptions is used instead, and the route
says so.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from ..discovery.llm_clients import LLMClient, LLMError
from ..discovery.llm_privacy import MaskedRequest, PlaceholderError, mask_request
from ..models.capability import Capability
from ..replay.replay_runner import validate_inputs

SYSTEM = """You route a request for a bank's back-office system to one existing capability.
Choose a capability only if it does exactly what the request asks; never one that does more
or something different. Values in the request are hidden behind placeholders like {value_1};
pass the placeholder as the input's value (words that are not hidden, as written). If no
capability fits, call no_matching_capability. If a capability fits but a required input is
missing from the request, call ask_clarification."""

NO_MATCH = {"name": "no_matching_capability", "description": "No capability does what is asked.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                           "required": ["reason"]}}
CLARIFY = {"name": "ask_clarification", "description": "A capability fits but an input is missing.",
           "parameters": {"type": "object", "properties": {
               "capability": {"type": "string"}, "question": {"type": "string"}},
               "required": ["capability", "question"]}}
STOP = {"the", "a", "an", "of", "for", "to", "and", "is", "what", "whats", "me", "my", "their",
        "his", "her", "please", "show", "get", "give", "tell", "with", "from", "in", "on", "at",
        "read", "look", "up", "member", "current", "this", "that", "it", "does", "do", "how",
        "much", "can", "you", "i", "new", "returns", "only", "given", "specific", "bank"}


@dataclass
class Route:
    kind: Literal["replay", "discover", "clarify"]
    capability: Capability | None = None
    params: dict[str, str] = field(default_factory=dict)
    how: Literal["llm", "keywords"] = "keywords"
    note: str = ""


def tool_for(cap: Capability) -> dict:
    outputs = ", ".join(f"{k} ({v.type})" for k, v in cap.outputs.items()) or "nothing"
    return {
        "name": cap.name,
        "description": (f"{cap.description} Returns: {outputs}. Risk: {cap.risk}. May instead "
                        f"answer with: {', '.join(cap.business_outcomes) or 'no business outcomes'}."),
        "parameters": {
            "type": "object",
            "properties": {k: {"type": "string", "description": v.description or k}
                           for k, v in cap.inputs.items()},
            "required": list(cap.inputs),
        },
    }


async def route(query: str, catalog: list[Capability], llm: LLMClient | None) -> Route:
    usable = [c for c in catalog if c.status == "approved"]
    if llm is not None and usable:
        try:
            return await _llm_route(mask_request(query), usable, llm)
        except LLMError as e:
            fallback = _keyword_route(query, usable)
            fallback.note = f"AI unavailable ({str(e)[:60]}...); matched by keywords. " + fallback.note
            return fallback
    if not usable:
        return Route("discover", note="no approved capabilities yet")
    return _keyword_route(query, usable)


async def _llm_route(request: MaskedRequest, usable: list[Capability], llm: LLMClient) -> Route:
    by_name = {c.name: c for c in usable}
    call = await llm.call_tool(SYSTEM, f"REQUEST: {request.text}", [tool_for(c) for c in usable]
                               + [NO_MATCH, CLARIFY])
    if call.name == "no_matching_capability":
        return Route("discover", how="llm", note=call.args.get("reason", ""))
    if call.name == "ask_clarification":
        return Route("clarify", by_name.get(call.args.get("capability", "")), how="llm",
                     note=call.args.get("question", ""))
    cap = by_name.get(call.name)
    if cap is None:  # the model can only choose from the list; anything else is rejected
        return Route("discover", how="llm", note=f"model named an unknown capability {call.name!r}")
    try:
        params = {k: request.resolve(str(v)) for k, v in call.args.items() if k in cap.inputs}
    except PlaceholderError as e:
        return Route("clarify", cap, how="llm", note=f"check the inputs: {e}")
    if problems := validate_inputs(cap, params):
        return Route("clarify", cap, params, how="llm", note="; ".join(problems))
    return Route("replay", cap, params, how="llm", note=f"the AI chose {cap.ref}")


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]+", text.lower().replace("_", " "))
            if len(w) > 1 and w not in STOP and _stem(w) not in STOP}


def _vocabulary(cap: Capability) -> set[str]:
    parts = [cap.name, cap.description]
    parts += [f"{k} {v.description}" for k, v in cap.inputs.items()]
    parts += [f"{k} {v.description}" for k, v in cap.outputs.items()]
    return _words(" ".join(parts))


def _keyword_route(query: str, usable: list[Capability]) -> Route:
    """Without the AI, be conservative: a close-but-wrong capability (savings instead of
    checking) is worse than asking. Reuse only when every word of the request is covered."""
    asked = _words(query)
    if not asked:
        return Route("discover", note="the request has no words to match on")
    ranked = []
    for cap in usable:
        covered = asked & _vocabulary(cap)
        ranked.append((len(covered) / len(asked), len(asked & _words(cap.name)), cap, asked - covered))
    ranked.sort(key=lambda r: (-r[0], -r[1]))
    coverage, _, best, missing = ranked[0]
    if coverage < 0.6:
        return Route("discover", note="no capability clearly matches this request")
    params = _extract(query, best)
    if coverage < 1:
        return Route("clarify", best, params, note=(
            f"Not sure {best.ref} is right: it does not mention {', '.join(sorted(missing))}. "
            "Check the inputs and run it, or learn a new capability instead."))
    if problems := validate_inputs(best, params):
        return Route("clarify", best, params, note="; ".join(problems))
    return Route("replay", best, params, note=f"matched {best.ref} by keywords (every word covered)")


def _extract(query: str, cap: Capability) -> dict[str, str]:
    """Best-effort inputs without an LLM: numbers for numeric inputs, amounts for decimals."""
    params: dict[str, str] = {}
    amounts = re.findall(r"\$?(\d[\d,]*\.\d{2})\b", query)
    numbers = [n for n in re.findall(r"\b\d{3,12}\b", query) if not any(n in a for a in amounts)]
    for name, spec in cap.inputs.items():
        if spec.type == "decimal" and amounts:
            params[name] = amounts.pop(0).replace(",", "")
        elif spec.pattern and re.search(r"\\d|\[0-9\]", spec.pattern) and numbers:
            params[name] = numbers.pop(0)
    return params
