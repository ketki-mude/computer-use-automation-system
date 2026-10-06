# invoked-as-agent-tool

An AI agent lists the approved capabilities as tools (GET /api/tools) and invokes one by name with typed inputs (POST /api/capabilities/get_savings_balance/invoke). It gets back the result contract. No LLM runs.

- expected: `success`, got: `success` (as expected)
- duration: 5.5 s (real timing, nothing slowed down)
- the bank afterwards: {'accounts_opened': 0, 'memberships_closed': 0}
