"""The app profile: everything shared by all capabilities of one app (sign-on, known
screens and dialogs, personal-data fields, version probe).
"""

from typing import Literal

from .capability import Condition, Model, OutcomeRule, Step


class DialogRule(Model):
    """A native browser dialog (alert/confirm) the app is known to show, and the answer."""
    when: str  # text the dialog's message contains
    action: Literal["accept", "dismiss"]

class VersionProbe(Model):
    frame: str | None = None
    regex: str  # group 1 is the version string

class PixelRegion(Model):
    """A pane of the app's window, for the pixel surface (which has no frames or DOM). Edges are
    window points; an unset edge is the window's edge, so a pane stretches with the window."""
    x0: float = 0
    y0: float = 0
    x1: float | None = None
    y1: float | None = None
    visible_when: str | None = None  # the pane exists only while this text is on screen


class AppProfile(Model):
    """Everything shared by all capabilities of one app: how to sign on, what its
    app-wide screens look like (session expired, maintenance notice, server error),
    and which PII formats it shows."""
    id: str
    vendor: str
    base_url: str
    entry_path: str = "/login"
    secrets: list[str] = []  # environment variables the profile may reference
    login: list[Step]
    signed_in: Condition
    version_probe: VersionProbe | None = None
    screens: dict[str, OutcomeRule] = {}
    # App-specific sensitive formats, by the placeholder the LLM sees instead (ACCOUNT -> <ACCOUNT>).
    # Also masked in every log line.
    pii_patterns: dict[str, str] = {}
    pii_fields: list[str] = []  # labels/columns whose values are personal data (see log_masking.py)
    dialogs: dict[str, DialogRule] = {}  # known native dialogs; any other is unexpected
    session_cookie: str | None = None  # lets a ticket name the app session it belongs to
    pixel_regions: dict[str, PixelRegion] = {}  # pane name (= the web frame name) -> region
