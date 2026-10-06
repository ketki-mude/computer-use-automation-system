"""Capability schema invariants."""

import pytest
from pydantic import ValidationError

from ui_automation.models.capability import OutputSpec, RoleName, Step, Target

from .capability_examples import make_capability


def test_capability_invariants():
    with pytest.raises(ValidationError, match="never extracted"):
        make_capability(outputs={"savings_balance": OutputSpec(), "other": OutputSpec()})
    with pytest.raises(ValidationError, match="riskiest step"):
        make_capability(steps=[Step(id="go", intent="commit", action="click", risk="irreversible",
                                target=Target(strategies=[RoleName(role="button", name="Confirm")])),
                           Step(id="read", intent="read", action="extract", output="savings_balance",
                                target=Target(strategies=[RoleName(role="cell", name="x")]))])
