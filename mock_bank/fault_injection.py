"""Injectable runtime faults, so replay error handling can be demonstrated on demand.

Counters are consumed one request at a time ("show the maintenance notice on the
next page load"), which keeps fault scenarios deterministic and repeatable.
Controlled via POST /__admin/faults, the control room's test controls, or `ui-automation fault`.
"""

import time
from dataclasses import asdict, dataclass


@dataclass
class Faults:
    maintenance: int = 0  # next N main-frame page loads show a "System Notice" interstitial
    verify_identity: int = 0  # next N sign-ons require a one-time code only a human has
    app_error: int = 0  # next N main-frame requests return a 500 error page
    slow_ms: int = 0  # added latency on every main-frame request
    expire_after: int = 0  # the session expires on the Nth main-frame request from now
    popup: int = 0  # next N main-frame pages raise a known alert ("password will expire")
    confirm_popup: int = 0  # next N main-frame pages raise an unknown confirm dialog
    unavailable: int = 0  # next N member-detail loads fail with a transient HTTP 503
    # 1 = the vendor's 7.5 release: "Member No." instead of "Member Number", a "Find" button
    # instead of "Search", a new version banner. Stays until reset, like an upgrade would.
    variant: int = 0
    sessions_expired_before: float = 0.0  # sessions created before this instant are expired

    def take(self, name: str) -> bool:
        """Consume one occurrence of a counted fault. Returns True if it fires."""
        remaining = getattr(self, name)
        if remaining > 0:
            setattr(self, name, remaining - 1)
            return True
        return False

    def update(self, settings: dict) -> None:
        for key, value in settings.items():
            if key == "expire_sessions":
                if value:
                    self.sessions_expired_before = time.time()
            elif key in ("maintenance", "verify_identity", "app_error", "slow_ms", "expire_after",
                         "popup", "confirm_popup", "unavailable", "variant"):
                setattr(self, key, int(value))
            else:
                raise ValueError(f"unknown fault: {key}")

    def as_dict(self) -> dict:
        return asdict(self)


FAULTS = Faults()


def reset() -> None:
    FAULTS.__init__()  # reset in place; other modules hold a reference to FAULTS
