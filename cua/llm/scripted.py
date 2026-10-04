"""A scripted stand-in for the LLM: for tests, offline demos, and reviewers without an API key.

It answers from a YAML script instead of a model. Each step names its target the way a
person would (role and name, or label, or cell text); the target is found on the screen
text in the prompt, so element numbers are never hard-coded. Everything around it (browser,
policy, recorder, compiler, replay) runs for real. Runs using it are marked model "scripted".
"""

import re
from pathlib import Path

import yaml

from .base import LLMError, T, ToolCall

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
        script = yaml.safe_load(Path(script_path).read_text())
        self.goal_spec = script["goal_spec"]
        self.steps = list(script["steps"])

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        return schema.model_validate(self.goal_spec)

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
