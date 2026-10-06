# staff-stops-run

Staff clicks Stop run on an irreversible capability. It stops at the next step boundary: STOPPED_BY_OPERATOR, nothing submitted.

- expected: `failed`, got: `failed` (as expected)
- duration: 2.9 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
