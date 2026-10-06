# transient-503

The member page fails once with HTTP 503; replay goes back and retries the navigation.

- expected: `success`, got: `success` (as expected)
- duration: 7.7 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
