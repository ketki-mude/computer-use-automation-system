"""Small capabilities built in code, shared by unit tests."""

from datetime import UTC, datetime

from ui_automation.models.capability import (
    AppRef,
    Capability,
    InputSpec,
    OutputSpec,
    Provenance,
    RoleName,
    Step,
    SuccessCheckpoint,
    Target,
)


def make_capability(**overrides) -> Capability:
    fields = {
        "name": "get_savings_balance", "version": "1.0.0", "description": "Read a balance.",
        "app": AppRef(id="acmecore_teller", vendor="acmecore_teller"), "risk": "read",
        "inputs": {"member_id": InputSpec(pattern=r"^\d{1,12}$", sensitivity="pii")},
        "outputs": {"savings_balance": OutputSpec(type="decimal", parse="currency")},
        "steps": [Step(id="read", intent="read it", action="extract", output="savings_balance",
                    target=Target(strategies=[RoleName(role="cell", name="x")]))],
        "success": SuccessCheckpoint(), "provenance": Provenance(recorded_at=datetime.now(UTC)),
    }
    fields.update(overrides)
    return Capability(**fields)


def make_read_capability(name, description, inputs, outputs, risk="read") -> Capability:
    return Capability(
        name=name, version="1.0.0", status="approved", description=description,
        app=AppRef(id="acmecore_teller", vendor="v"), risk=risk,
        inputs={k: InputSpec(pattern=r"^[0-9]{1,12}$") if k.endswith("number") else InputSpec()
                for k in inputs},
        outputs={k: OutputSpec(type="decimal") for k in outputs},
        steps=[Step(id=f"read_{o}", intent="read", action="extract", output=o,
                    target=Target(strategies=[RoleName(role="cell", name="x")])) for o in outputs],
        success=SuccessCheckpoint(), provenance=Provenance(recorded_at=datetime.now(UTC)))


SAVINGS = make_read_capability("get_savings_balance", "Read-only. Retrieves the current savings account balance for a member.",
              ["member_number"], ["savings_balance"])
