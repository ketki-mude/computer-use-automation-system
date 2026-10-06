# stuck-then-resumed

An unknown dialog is cancelled and the run raises a stuck ticket. The operator resumes and the run finishes.

- expected: `success`, got: `success` (as expected)
- duration: 6.6 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
