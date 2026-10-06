"""What a model is allowed to see.

The model plans; code handles the data. Before any text reaches a model:

- Values in the request (member numbers, amounts, emails) become placeholders: {value_1},
  {value_2}. Once the model has named the inputs they read {member_number}, and when the
  model types a placeholder, code types the real value.
- On screen, a known input value becomes its placeholder ({member_number}). App-specific
  formats become their label (<ACCOUNT>, <MONEY>). Values the run has registered as
  sensitive (personal-data fields, extracted outputs) become <VALUE>, secrets <SECRET>,
  dates <DATE>, and any other number of four or more digits <NUMBER>. Category labels
  (REGULAR SAVINGS) and the names of controls stay readable, so the model can still choose
  the right row and button.
- Outputs are captured by pointer: the model names the cell, code reads it, and the model
  is only told that the value was captured.

`ScreenMasker.text` is applied to the whole prompt as a last pass, so a value that slipped
into a history line or a dialog message is caught too.
"""

import re
from dataclasses import dataclass, field

from ..safety.log_masking import GENERIC_PATTERNS, Redactor

REQUEST_VALUE = re.compile(
    r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"  # email
    r"|(?<![\w])\$?\d[\d,]*(?:\.\d+)?"  # number or amount (not the digits inside a word)
)
REQUEST_PLACEHOLDER = re.compile(r"\{?(value_\d+)\}?")
INPUT_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
DATE = re.compile(r"(?<![\w*])(?:\d{1,2}|\*\*)/(?:\d{1,2}|\*\*)/\d{2,4}(?![\w])")
LONG_NUMBER = re.compile(r"(?<![\w])\d{4,}(?![\w])")
HIDDEN = re.compile(r"<[A-Z]+>")
SENSITIVE_KINDS = ("pii", "financial")


class PlaceholderError(ValueError):
    """The model used a placeholder that does not exist."""


@dataclass
class MaskedRequest:
    """A request with its values replaced by {value_n} placeholders."""
    text: str
    values: dict[str, str] = field(default_factory=dict)  # "value_1" -> "12345"

    def resolve(self, given: str) -> str:
        """The model's answer for an input -> the real value. A placeholder ({value_1}) is
        swapped for the value it hides; plain words (REGULAR SAVINGS) were visible to the
        model and are kept as written."""
        given = given.strip()
        if m := REQUEST_PLACEHOLDER.fullmatch(given):
            if m.group(1) not in self.values:
                raise PlaceholderError(f"the request has no {{{m.group(1)}}}")
            return self.values[m.group(1)]
        return given

    def named(self, names: dict[str, str]) -> str:
        """The request text with {value_n} replaced by input names: {member_number}.
        `names` maps value_n -> input name."""
        return REQUEST_PLACEHOLDER.sub(lambda m: "{" + names.get(m.group(1), m.group(1)) + "}",
                                       self.text)


def mask_request(text: str) -> MaskedRequest:
    """Hide every number, amount and email in a request before a model reads it."""
    values: dict[str, str] = {}

    def hide(m: re.Match) -> str:
        raw = m.group(0)
        if raw.endswith((".", ",")):  # sentence punctuation, not part of the value
            return hide_value(raw[:-1]) + raw[-1]
        return hide_value(raw)

    def hide_value(raw: str) -> str:
        value = raw.lstrip("$").replace(",", "") if raw[:1] in "$0123456789" else raw
        for key, existing in values.items():
            if existing == value:
                return "{" + key + "}"
        key = f"value_{len(values) + 1}"
        values[key] = value
        return "{" + key + "}"

    return MaskedRequest(REQUEST_VALUE.sub(hide, text), values)


def fill_placeholders(text: str, inputs: dict[str, str]) -> str:
    """What code types when the model types `text`: each {input_name} becomes its value."""
    def fill(m: re.Match) -> str:
        if m.group(1) not in inputs:
            known = ", ".join("{" + k + "}" for k in inputs) or "none"
            raise PlaceholderError(f"unknown placeholder {m.group(0)} (inputs: {known})")
        return inputs[m.group(1)]
    return INPUT_PLACEHOLDER.sub(fill, text)


def readable_part(option: str) -> str:
    """For a dropdown choice the model copied with hidden parts
    ("<ACCOUNT> SHARE DRAFT CHECKING (<MONEY> avail)"), the longest readable fragment
    ("SHARE DRAFT CHECKING"). Options are matched by containment, so that is enough."""
    if not HIDDEN.search(option):
        return option
    return max((part.strip(" ()[]-,:") for part in HIDDEN.split(option)), key=len)


def _whole(values: list[str]) -> re.Pattern | None:
    if not values:
        return None
    alternatives = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
    return re.compile(rf"(?<![\w])(?:{alternatives})(?![\w])")


class ScreenMasker:
    """Turns screen text, history lines and whole prompts into what a model may see."""

    def __init__(self, inputs: dict[str, str], redactor: Redactor,
                 app_patterns: dict[str, str] | None = None):
        self.inputs = {k: v for k, v in inputs.items() if v}
        self.redactor = redactor
        self.app_patterns = [(re.compile(p), f"<{label}>") for label, p in (app_patterns or {}).items()]
        self.generic = [re.compile(p) for p in GENERIC_PATTERNS]

    def text(self, s: str) -> str:
        registered = self.redactor.registered()
        input_values = set(self.inputs.values())
        # Order matters: secrets, then whole formats (an account number before the member
        # number inside it), then inputs, then anything else sensitive.
        secrets = _whole([v for v, kind in registered.items() if kind == "secret"])
        if secrets:
            s = secrets.sub("<SECRET>", s)
        for pattern, label in self.app_patterns:
            s = pattern.sub(label, s)
        by_value = {v: k for k, v in self.inputs.items()}
        inputs = _whole(list(by_value))
        if inputs:
            s = inputs.sub(lambda m: "{" + by_value[m.group(0)] + "}", s)
        others = _whole([v for v, kind in registered.items()
                         if kind in SENSITIVE_KINDS and v not in input_values])
        if others:
            s = others.sub("<VALUE>", s)
        for pattern in self.generic:
            s = pattern.sub("<VALUE>", s)
        s = DATE.sub("<DATE>", s)
        return LONG_NUMBER.sub("<NUMBER>", s)
