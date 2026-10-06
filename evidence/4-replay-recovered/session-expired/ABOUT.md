# session-expired

The session expires mid-run; replay signs on again and restarts (safe: nothing was committed).

- expected: `success`, got: `success` (as expected)
- duration: 7.9 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
