"""The policy enforcement point's rules: which origins, routes and action types are allowed,
and how risky each action is. The surface adapter calls `authorize` before every action
and installs `allows_url` as a network-level filter, so neither the LLM nor a recorded
capability can act outside the allowlist.
"""

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from pydantic import Field

from .config import CONFIG_DIR
from .schema import Model, Risk

ONCLICK_URL = re.compile(r"""location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""")


class Policy(Model):
    allowed_origins: list[str]
    allowed_paths: list[str]
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str]
    irreversible_keywords: list[str]
    read_keywords: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        path = path or CONFIG_DIR / "policy.yaml"
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def allows_url(self, url: str) -> tuple[bool, str]:
        u = urlparse(url)
        if u.scheme in ("about", "data", "blob"):
            return True, "local"
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.allowed_origins:
            return False, f"origin {origin} is not allowlisted"
        if any(fnmatch(u.path, p) for p in self.denied_paths):
            return False, f"route {u.path} is denied"
        if not any(fnmatch(u.path, p) for p in self.allowed_paths):
            return False, f"route {u.path} is not allowlisted"
        return True, "allowed"

    def classify(self, action: str, role: str, name: str, onchange: str = "") -> Risk:
        if action == "extract":
            return "read"
        if action in ("fill", "select"):
            # Typing changes nothing on the server until something is submitted, unless the
            # field submits by itself (legacy "auto postback" fields carry an onchange handler).
            return "reversible" if onchange else "read"
        words = set(re.findall(r"[a-z]+", name.lower()))
        if words & set(self.irreversible_keywords):
            return "irreversible"
        if role == "link" or words & set(self.read_keywords):
            return "read"
        return "reversible"

    def authorize(self, action: str, meta: dict, frame_url: str) -> "Decision":
        """Check one concrete action against the allowlist. `meta` describes the element."""
        risk = self.classify(action, meta.get("role", ""), meta.get("name", ""),
                             meta.get("onchange", ""))
        if action not in self.allowed_actions:
            return Decision(False, risk, f"action '{action}' is not allowlisted")
        for raw in (meta.get("href"), *ONCLICK_URL.findall(meta.get("onclick") or "")):
            if raw and not raw.startswith(("#", "javascript:")):
                ok, why = self.allows_url(urljoin(frame_url, raw))
                if not ok:
                    return Decision(False, risk, f"target navigates outside the allowlist: {why}")
        return Decision(True, risk, "allowed")


@dataclass
class Decision:
    allowed: bool
    risk: Risk
    reason: str
