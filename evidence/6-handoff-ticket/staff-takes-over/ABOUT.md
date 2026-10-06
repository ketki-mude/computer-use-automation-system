# staff-takes-over

Nobody asked for help: staff clicks Take over while the run is going. It pauses at the next step boundary (never mid-step) and raises a takeover ticket; the operator takes it, then resumes.

- expected: `success`, got: `success` (as expected)
- duration: 6.5 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
