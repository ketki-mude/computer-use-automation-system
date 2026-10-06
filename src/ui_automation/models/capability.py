"""The capability recipe: a contract the caller sees, plus a flow only replay reads.

A capability has two halves:
- a *contract* the calling agent sees: name, description, typed inputs and outputs,
  risk, and the outcomes it can return;
- a *flow* only the replay engine reads: ordered steps, each with a ranked list of
  locator strategies, an optional postcondition, and a risk class.

Nothing here knows about Playwright. Strategies describe *what* to find in terms a
human would use ("the textbox in the same row as 'Member Number'"); a surface
adapter decides *how*. That is the seam that lets a desktop or pixel adapter reuse
the same flow later.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Risk = Literal["read", "reversible", "irreversible"]

Sensitivity = Literal["public", "internal", "pii", "financial", "secret"]

RISK_ORDER: dict[str, int] = {"read": 0, "reversible": 1, "irreversible": 2}

class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

class RoleName(Model):
    """A control with an accessible role and name, e.g. button "Search"."""
    kind: Literal["role_name"] = "role_name"
    role: str
    name: str

class LabelAnchor(Model):
    """A control (or value cell) found by the visible text beside it in the same table row.
    Made for legacy table layouts that have no <label> elements."""
    kind: Literal["label_anchor"] = "label_anchor"
    label: str
    role: str  # textbox | combobox | checkbox | cell ...

class RowMatch(Model):
    column: str  # header text of the column to match on
    equals: str  # literal or ${param}

class TableRow(Model):
    """A control inside the data-table row whose `column` cell equals a value."""
    kind: Literal["table_row"] = "table_row"
    row: RowMatch
    role: str
    name: str

class TableCell(Model):
    """The cell under `column` in the data-table row picked by `row`."""
    kind: Literal["table_cell"] = "table_cell"
    row: RowMatch
    column: str

class Text(Model):
    """The innermost element whose text equals `text`."""
    kind: Literal["text"] = "text"
    text: str
    role: str | None = None

class Css(Model):
    """Last resort. Only stable attributes (e.g. a control's name) are ever recorded here."""
    kind: Literal["css"] = "css"
    selector: str

Strategy = Annotated[RoleName | LabelAnchor | TableRow | TableCell | Text | Css,
                     Field(discriminator="kind")]

class Fingerprint(Model):
    """What the element looked like at discovery. Used to confirm a match, never to find one."""
    tag: str
    role: str
    label: str = ""
    text: str = ""

class Target(Model):
    frame: str | None = None  # frame name; None means the top-level document
    strategies: list[Strategy] = Field(min_length=1)
    fingerprint: Fingerprint | None = None

class Condition(Model):
    """A fact about the current screen. All fields that are set must hold."""
    text: str | None = None  # substring of the visible text (of `frame`, or of any frame)
    regex: str | None = None  # regex on visible text; group 1, if any, becomes the message
    title_contains: str | None = None  # the frame's document title
    frame_present: str | None = None  # a frame with this name exists
    frame: str | None = None

    @model_validator(mode="after")
    def _not_empty(self):
        if not (self.text or self.regex or self.title_contains or self.frame_present):
            raise ValueError("a condition needs text, regex, title_contains or frame_present")
        return self

class RecoveryAction(Model):
    # click: press a control (dismiss a notice); relogin_restart: sign on again and start over;
    # retry_previous: go back and redo the navigation that failed (a transient page error).
    action: Literal["click", "relogin_restart", "retry_previous"]
    target: Target | None = None

class OutcomeRule(Model):
    """A known screen state and what replay should do when it sees it.

    business     a legitimate answer the caller must handle ("no such member")
    recoverable  handled inside replay ("dismiss the maintenance notice")
    failure      stop with a debuggable error ("server error page")
    escalate     a person must act on the live session ("identity code")
    """
    kind: Literal["business", "recoverable", "failure", "escalate"]
    when: Condition
    message: str | None = None
    description: str | None = None  # for the capability contract: when this outcome happens
    recovery: RecoveryAction | None = None
    # For "escalate": the steps a person follows, shown on the ticket. May use {session}.
    instructions: list[str] = []

    @model_validator(mode="after")
    def _recovery_matches_kind(self):
        if self.kind == "recoverable" and self.recovery is None:
            raise ValueError("a recoverable outcome needs a recovery")
        return self

class InputSpec(Model):
    type: Literal["string", "integer", "decimal"] = "string"
    description: str = ""
    pattern: str | None = None
    sensitivity: Sensitivity = "internal"

class OutputSpec(Model):
    type: Literal["string", "integer", "decimal"] = "string"
    description: str = ""
    parse: Literal["text", "currency", "integer"] = "text"
    sensitivity: Sensitivity = "internal"

class Expect(Model):
    """Postcondition checked after the step acts."""
    title_contains: str | None = None
    text: str | None = None
    frame: str | None = None

class Step(Model):
    id: str
    intent: str  # why this step exists, in plain words, for reviewers
    action: Literal["click", "fill", "select", "extract"]
    target: Target
    value: str | None = None  # fill/select value: literal, ${param} or ${secret:NAME}
    output: str | None = None  # extract: which declared output receives the value
    risk: Risk = "read"
    expect: Expect | None = None
    timeout_s: float = 10.0
    provenance: Literal["llm", "human", "authored"] = "llm"

    @model_validator(mode="after")
    def _fields_match_action(self):
        if self.action in ("fill", "select") and self.value is None:
            raise ValueError(f"step {self.id}: {self.action} needs a value")
        if self.action == "extract" and not self.output:
            raise ValueError(f"step {self.id}: extract needs an output")
        return self

class SuccessCheckpoint(Model):
    title_contains: str | None = None
    text: str | None = None
    frame: str | None = None
    outputs_present: bool = True

class AppRef(Model):
    id: str  # app profile id, e.g. acmecore_teller
    vendor: str  # vendor product shared by many tenants, e.g. acmecore_teller
    versions: str = "*"  # vendor versions this capability is known to work on

class Provenance(Model):
    discovered_by_run: str | None = None
    model: str | None = None
    recorded_at: datetime
    validated_by_run: str | None = None  # the last passing validation replay
    validation_runs: list[str] = []  # every validation replay, all passed (the stability evidence)
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None

class Capability(Model):
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["draft", "approved", "rejected", "deprecated"] = "draft"
    description: str
    app: AppRef
    surface: Literal["web", "desktop", "pixel"] = "web"
    requires: list[str] = ["signed_in"]  # preconditions the app profile establishes
    risk: Risk
    inputs: dict[str, InputSpec] = {}
    outputs: dict[str, OutputSpec] = {}
    # The business answers a caller must be ready for, by code (part of the contract).
    business_outcomes: dict[str, str] = {}
    steps: list[Step] = Field(min_length=1)
    outcomes: dict[str, OutcomeRule] = {}  # capability-specific detectors; app-wide ones live in the profile
    success: SuccessCheckpoint
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self):
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        extracted = {s.output for s in self.steps if s.action == "extract"}
        if missing := set(self.outputs) - extracted:
            raise ValueError(f"outputs never extracted: {sorted(missing)}")
        worst = max((RISK_ORDER[s.risk] for s in self.steps), default=0)
        if RISK_ORDER[self.risk] < worst:
            raise ValueError("capability risk is lower than its riskiest step")
        return self

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"
