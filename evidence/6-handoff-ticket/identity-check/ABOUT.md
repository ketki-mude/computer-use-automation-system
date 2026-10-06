# identity-check

Sign-on asks for a one-time code only a person has. A ticket is raised; the operator takes it, types the code on the same live session and resumes; automation finishes.

- expected: `success`, got: `success` (as expected)
- duration: 7.3 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
