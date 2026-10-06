# approved

Automation fills the form and stops before Confirm. The operator checks the paused screen (view only) and approves; automation clicks Confirm and reads the confirmation number.

- expected: `success`, got: `success` (as expected)
- duration: 10.2 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 1, 'memberships_closed': 0}
