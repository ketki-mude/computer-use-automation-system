# Computer-Use Automation System

An LLM learns a back-office task once by driving the real screen. The run is compiled into a typed, versioned **capability** (a YAML file a person reviews). From then on the capability is **replayed deterministically with no LLM**, it tells business answers apart from real failures, and it can hand the live browser session to a person when it gets stuck.

The target is a deliberately hostile mock of a legacy core-banking app (framesets, table layouts, no labels or test IDs, element ids that change on every render, errors returned as normal pages), with faults you can inject on demand. All data is synthetic.

The design and its trade-offs are in [REPORT.md](REPORT.md). Recorded runs are in [evidence/](evidence/README.md).

## Setup

Requires Python 3.11+ (tested on 3.14).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"          # or: uv sync --extra dev
.venv/bin/playwright install chromium
cp .env.example .env                       # then fill in GEMINI_API_KEY (optional, see below)
source .venv/bin/activate
```

| Variable | Needed for | Default |
|---|---|---|
| `GEMINI_API_KEY` | live discovery only. Replay never calls an LLM. | none |
| `CUA_DISCOVERY_MODEL` | discovery model | `gemini-3.8-flash` |
| `CUA_FALLBACK_MODELS` | tried in order when the model is overloaded or out of quota | `gemini-3.5-flash,gemini-3-flash-preview` |
| `MOCKBANK_USER`, `MOCKBANK_PASSWORD` | the mock bank sign-on, read through secret references | `teller01` / `Demo#2026` |
| `CUA_OPERATOR_PORT`, `CUA_CDP_PORT` | operator console and the browser's local debugging port during `--operator` runs | `8001`, `9222` |

## Demo path

Start the target app in one terminal and leave it running:

```bash
mockbank serve                    # http://127.0.0.1:8000  (test harness: /__admin)
```

In a second terminal:

```bash
# 1. Discover: the LLM accomplishes the goal; the run is compiled, validated by a
#    replay in a fresh browser, and saved as a draft capability.
cua discover "look up member 12345 and read their current savings balance"

# 2. Review the artifact, then approve it for unattended replay.
cua capabilities
cua show get_savings_balance
cua approve get_savings_balance

# 3. Replay: no LLM anywhere. Prints the result contract as JSON.
cua replay get_savings_balance -p member_number=12345      # success, savings_balance "1204.50"
cua replay get_savings_balance -p member_number=23456      # another member, same capability
cua replay get_savings_balance -p member_number=99999      # business outcome RECORD_NOT_FOUND
cua replay get_savings_balance -p member_number=70000      # failed APP_ERROR, with screenshot + DOM + trace

# 4. Runtime errors on demand.
mockbank fault maintenance=1;   cua replay get_savings_balance -p member_number=12345   # recovered
mockbank fault expire_after=2;  cua replay get_savings_balance -p member_number=12345   # re-sign-on + restart

# 5. Human handoff on the live session.
mockbank fault verify_identity=1
cua replay get_savings_balance -p member_number=12345 --operator
#    Open http://127.0.0.1:8001 and click "Take control". In the browser window the run opened,
#    type the one-time code shown at http://127.0.0.1:8000/__admin and click Verify.
#    Back in the console click "Resume automation"; the replay finishes the task.

# 6. The irreversible gate.
cua replay open_sub_account -p member_number=23456 -p share_type="REGULAR SAVINGS" -p initial_deposit=100.00
#    -> failed APPROVAL_REQUIRED, nothing touched. Add --allow-irreversible to commit.
```

The repo ships both capabilities already approved ([capabilities/](capabilities/)), so step 3 onward works without running discovery first. A fresh discovery creates the next version (for example 1.1.0) as a draft; replay always takes the latest version, so approve the new one or pass `--version 1.0.0`. The input's name is part of the contract chosen at discovery; `cua capabilities` shows it.

Every run writes its evidence to `runs/<run-id>/` (gitignored): a redacted `events.jsonl`, plus screenshots, a DOM snapshot and a Playwright trace on failure. Add `--headed` to any command to watch the browser.

**Replay exit codes:** 0 success or business outcome, 1 failed, 3 escalated.

## Running without live services

- **Replay never needs an LLM or a network connection.** Only the local mock bank is required.
- **Discovery can run offline** from a scripted decision file. The browser, policy, recorder, compiler and validation replay all run for real; only the decisions come from the script, and the run is marked `model: scripted`:

  ```bash
  cua discover "look up member 12345 and read their current savings balance" \
      --script demo/get_savings_balance.script.yaml
  ```

- `python scripts/make_evidence.py` runs all 13 replay scenarios (successes, business outcomes, recoveries, failures, the handoff, the irreversible gate) with no LLM, and refreshes `evidence/`.

## Tests

```bash
pytest          # 23 tests: compiler rules, policy, redaction, input and output contracts, mock bank faults
ruff check .
```

## Layout

```
cua/
  agent.py        discovery loop: observe -> decide (Gemini tool call) -> act -> record
  compiler.py     trace -> capability: prune, parameterize, generalize locators, postconditions
  replay.py       deterministic replay, error taxonomy, recoveries, commit point
  executor.py     shared: waiting, known-screen detectors, secrets, sign-on
  session.py      control lease, interventions, operator console
  schema.py       capability, app profile and result contract (Pydantic)
  policy.py       allowlist and risk classes
  redact.py       redaction of everything written to disk
  surface/        web adapter (Playwright) and dom.js, shared by recording and replay
  llm/            LLM seam: Gemini client and the scripted client
config/           policy.yaml (allowlist) and apps/legacy_core.yaml (app profile)
capabilities/     saved capabilities, one YAML file per version
mockbank/         the target app and its fault injection
scripts/          evidence runner, demo operator, evidence scrubber
evidence/         recorded discovery and replay runs
```
