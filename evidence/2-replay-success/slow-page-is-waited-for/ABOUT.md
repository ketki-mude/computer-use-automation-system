# slow-page-is-waited-for

A 4 s server delay. Replay waits on the page's own conditions; there are no fixed sleeps.

- expected: `success`, got: `success` (as expected)
- duration: 9.1 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
