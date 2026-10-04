"""Redaction for everything written to disk: event logs, observations, DOM snapshots.

Two layers:
- known values: secrets and sensitive inputs/outputs registered during a run are masked
  wherever they appear (whole-token match, so "12345" never half-masks "123456");
- patterns: generic PII formats (SSN, card, email) plus app-specific ones from the
  app profile (e.g. account numbers).

Limits: free text the system was never told about (a member's name on screen) is not
caught by either layer. Screenshots are handled separately by masking elements.
"""

import re
from typing import Any

GENERIC_PATTERNS = [
    r"\b\d{3}-\d{2}-\d{4}\b",  # SSN
    r"\b(?:\d{4}[ -]){3}\d{4}\b|\b\d{16}\b",  # card number
    r"\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",  # email
]
SECRET_MASK = "••••"


def mask_value(value: str, sensitivity: str) -> str:
    if sensitivity == "secret":
        return SECRET_MASK
    return "***" + value[-2:] if len(value) > 2 else "***"


class Redactor:
    def __init__(self, extra_patterns: list[str] | None = None):
        self._values: dict[str, str] = {}
        self._patterns = [re.compile(p) for p in GENERIC_PATTERNS + (extra_patterns or [])]
        self._value_re: re.Pattern | None = None

    def register(self, value: Any, sensitivity: str) -> None:
        """Mask this value from now on. Public and internal values are left alone."""
        if sensitivity not in ("pii", "financial", "secret") or value is None:
            return
        text = str(value)
        if len(text) < 2:
            return
        self._values[text] = mask_value(text, sensitivity)
        alternatives = "|".join(re.escape(v) for v in sorted(self._values, key=len, reverse=True))
        self._value_re = re.compile(rf"(?<![\w]){'(?:' + alternatives + ')'}(?![\w])")

    def sensitive_values(self) -> list[str]:
        """Registered values that are not secrets, for masking screenshots."""
        return [v for v, mask in self._values.items() if mask != SECRET_MASK]

    def text(self, s: str) -> str:
        # Patterns first, so a whole account number is masked before a member id inside it.
        for pattern in self._patterns:
            s = pattern.sub(lambda m: "*" * min(len(m.group(0)), 12), s)
        if self._value_re is not None:
            s = self._value_re.sub(lambda m: self._values[m.group(0)], s)
        return s

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj
