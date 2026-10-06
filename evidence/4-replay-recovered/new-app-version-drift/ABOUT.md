# new-app-version-drift

The vendor's 7.5 release renamed 'Member Number' and 'Search'. Replay finds both by their fallback locators, finishes, and reports each as drift, plus the version outside 7.4.*.

- expected: `success`, got: `success` (as expected)
- duration: 5.3 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
