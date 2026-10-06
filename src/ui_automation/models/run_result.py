"""The replay result contract: success, business_outcome, failed or escalated, with the
details a caller (or a person debugging) needs.
"""

from typing import Any, Literal

from .capability import Model

ErrorKind = Literal[
    "INPUT_INVALID",  # caller sent inputs that fail the capability's schema
    "TARGET_NOT_FOUND",  # no strategy matched an element
    "TARGET_AMBIGUOUS",  # strategies matched several elements and none matched exactly one
    "CHECKPOINT_FAILED",  # a postcondition or the success checkpoint never held
    "APP_ERROR",  # the app showed a known error screen
    "POLICY_BLOCKED",  # the action was outside the allowlist
    "APPROVAL_REQUIRED",  # irreversible step without the caller's explicit approval
    "UNKNOWN_STATE",  # the screen matched nothing known
    "ESCALATION_UNRESOLVED",  # a human was needed and nobody resolved it
    "STOPPED_BY_OPERATOR",  # staff stopped the run (between steps, never in the middle of one)
    "SURFACE_ERROR",  # the browser or page itself failed
]

class BusinessOutcome(Model):
    code: str
    message: str
    step_id: str | None = None

class Recovery(Model):
    step_id: str | None
    condition: str
    action: str

class HandoffRecord(Model):
    intervention_id: str
    reason: str
    operator: str | None = None
    resolution: Literal["resumed", "completed_by_human", "approved", "rejected", "aborted", "timed_out"]
    human_actions: int = 0

class Drift(Model):
    step_id: str
    strategy_used: int  # 0 is the primary strategy
    note: str

class RunError(Model):
    kind: ErrorKind
    step_id: str | None = None
    expected: str
    observed: str
    retryable: bool = False
    screenshot: str | None = None
    dom_snapshot: str | None = None

class RunResult(Model):
    status: Literal["success", "business_outcome", "failed", "escalated"]
    capability: str
    run_id: str
    outputs: dict[str, Any] = {}
    outcome: BusinessOutcome | None = None
    error: RunError | None = None
    recoveries: list[Recovery] = []
    handoffs: list[HandoffRecord] = []
    drift: list[Drift] = []
    duration_ms: int = 0
    evidence_dir: str = ""
