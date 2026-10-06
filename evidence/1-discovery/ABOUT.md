# 1-discovery

Discovery of a task the catalog does not have: “Read the checking account available balance for member 12345”. The model sees placeholders, never member data (see discovery-run/observations, and llm.jsonl when a real model decided). The run is compiled, must pass three validation replays in fresh browsers, and is saved as a draft.

- expected: `discovered`, got: `discovered` (as expected)
- duration: 41.5 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
- discovery decisions by: gpt-6-luna
