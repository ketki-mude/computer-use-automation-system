# no-retry-after-commit

The session expires on the Confirm request. The account may exist, so replay does not retry: ESCALATION_UNRESOLVED.

- expected: `failed`, got: `failed` (as expected)
- duration: 8.4 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
