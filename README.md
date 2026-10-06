# UI Automation System

An LLM learns a back-office task once by driving the real screen. The run is compiled into a typed, versioned **capability** (a YAML recipe a person reviews and approves). From then on the capability is **replayed deterministically with no LLM**. Replay tells business answers ("no such member") apart from real failures, recovers from known problems by itself, and hands the live session to a person through a ticket when it cannot continue.

The target is a deliberately hostile mock of a legacy core-banking app: framesets, table layouts with no labels or test IDs, element ids that change on every render, JavaScript navigation, and errors returned as normal pages. Faults can be injected on demand. All data is synthetic. **The LLM never sees member data**: numbers, balances, account numbers, names and passwords reach it only as placeholders such as `{member_number}`, `<MONEY>` and `<ACCOUNT>`.

The design and its trade-offs are in [REPORT.md](REPORT.md). Recorded runs are in [evidence/](evidence/README.md).

## Setup

Works on Windows, macOS and Linux with Python 3.11 or newer.

```bash
python3 -m venv .venv            # Windows: py -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"           # add ,pixel for the no-DOM surface: ".[dev,pixel]"
playwright install chromium
ui-automation demo
```

The `pixel` extra installs a pip-only OCR engine (RapidOCR with ONNX Runtime) for replay with no DOM; everything else works without it.

`ui-automation demo` starts the mock bank and the web app, and opens two pages:

- **Member requests** (`http://127.0.0.1:8001/ask`): the requester's view. Ask in plain words and get the answer; a *History* list keeps earlier questions. It shows only the answer and the steps in plain words, never the teller screen (that screen shows other members and accounts).
- **Control room** (`http://127.0.0.1:8001/control`): the staff view. Tabs: **Tickets** (with a count badge; each ticket walks you through *take it, do the step on the screen, hand it back*), **Activity** (every request, its steps and live screen, with *Take over* and *Stop*), **Learned tasks** (the capabilities, as a plain-English review card with Approve / Reject), and **Demo tools** (make the next request hit a problem, set the speed).

No API key is needed. Requests that match an approved capability replay with no AI, and the three offline demo requests learn from scripted decisions. To let Gemini learn new tasks and choose capabilities, copy `.env.example` to `.env` and set `GEMINI_API_KEY`. Runs are at normal speed by default (real timings); `ui-automation demo --slow`, or the speed setting in Demo tools, slows every browser action down so you can follow it.

## A five-minute tour

1. **Reuse, no AI.** On the requests page, ask *What's the savings balance for member 23456?* It is routed to the approved `get_savings_balance` capability and replayed step by step; the answer is 15002.75.
2. **A business answer.** Ask *Savings balance for member 99999*. The answer is RECORD_NOT_FOUND: a legitimate result, not a crash.
3. **Recovery.** In the control room, open Demo tools and click *Maintenance notice* (or *Session expires halfway*, or *Page fails to load once*). Ask again: the request deals with it by itself, and Activity shows what it handled.
4. **A ticket.** In Demo tools click *Asks for a one-time code*, type your name at the top of the control room, and ask again. The request pauses and **Tickets (1)** lights up. Click *Take this ticket*, get the code from the supervisor's device (linked in the instructions), click the code box on the screen, type the code under the screen and press *Type*, click Verify, then *Hand back to automation*. The requests page gets its answer.
5. **An approval.** Ask *Open a REGULAR SAVINGS sub-account for member 23456 with an initial deposit of 100.00 from checking*. Automation fills the form and stops before Confirm. Take the approval ticket, click Confirm on the screen, then *I clicked it, carry on*; or click *Don't do it*, and nothing is submitted.
6. **Learn something new.** Ask *Read the checking account available balance for member 12345*. No capability does this, so it is learned (by Gemini, or by the offline script), must pass three validation replays, and is saved as a draft. Open Learned tasks, read the review card, and Approve. Ask the same for member 23456: it is now reused with no AI.
7. **Step in without being asked.** Ask anything, open it in Activity, and click *Take over*. The request pauses after its current step and opens a ticket for you; *Stop* ends it the same way, never in the middle of a step.
8. **A new app version.** In Demo tools, click *New version of the bank app*: the bank renames "Member Number" and "Search". Ask again: replay finds both by their fallback locators, answers, and reports the drift (and that 7.5 is outside the versions the capability was proven on).
9. **No DOM at all.** With the `pixel` extra, run `ui-automation replay get_savings_balance -p member_number=23456 --surface pixel` (with the demo running). The same capability replays from screenshots alone: OCR to read, mouse and keys to act. Add `--window 1100x760 --scale 1.5` to see it work in another window size and display scale.

## Command line

| Command | What it does |
|---|---|
| `ui-automation demo [--slow]` | the requests page, the control room and the mock bank |
| `ui-automation discover "goal" [--script FILE]` | learn a capability (Gemini, or a scripted decision file offline); saved as a draft after a validation replay |
| `ui-automation replay NAME -p key=value [--json]` | replay with no LLM and print the result contract |
| `ui-automation capabilities` / `show NAME` | list capabilities / print one |
| `ui-automation approve NAME` / `reject NAME -r "why"` | review a draft |
| `ui-automation replay NAME --surface pixel [--window WxH --scale S]` | the same replay with no DOM: screenshots and OCR, mouse and keys |
| `ui-automation fault KEY=VALUE` / `reset-bank` | inject a fault into the running mock bank / restore it |
| `ui-automation schemas` | write the JSON Schemas (capability, result contract, app profile) to `schemas/` |

For example, with `ui-automation demo` running in another terminal:

```bash
ui-automation replay get_savings_balance -p member_number=12345    # success: savings_balance 1204.50
ui-automation replay get_savings_balance -p member_number=70000    # failed APP_ERROR, masked screenshot + DOM
ui-automation fault expire_after=2
ui-automation replay get_savings_balance -p member_number=12345    # recovered: signs on again, restarts
ui-automation replay open_sub_account -p member_number=23456 -p share_type="REGULAR SAVINGS" -p initial_deposit=100.00
                                                                   # failed APPROVAL_REQUIRED: nothing touched
ui-automation discover "Read the checking account available balance for member 12345" \
    --script scripted_discovery/get_checking_available_balance.yaml
```

Add `--headed` to watch the browser, or `--operator` to send tickets to the control room. Replay exit codes: 0 success or business outcome, 1 failed, 3 escalated. Every run writes masked evidence to `runs/<run-id>/` (gitignored).

## For an AI agent

Approved capabilities are offered as tools over HTTP while `ui-automation demo` runs:

```bash
curl http://127.0.0.1:8001/api/tools        # function-calling definitions: name, description, typed inputs
curl -X POST http://127.0.0.1:8001/api/capabilities/get_savings_balance/invoke \
     -H 'content-type: application/json' -d '{"inputs": {"member_number": "23456"}}'
```

The response is the result contract ([schemas/run_result.schema.json](schemas/run_result.schema.json)). If the run is waiting for a person, the answer is `202` with the job to poll.

## Configuration

All optional; see [.env.example](.env.example).

| Variable | Used for | Default |
|---|---|---|
| `GEMINI_API_KEY` | discovery and request routing. Replay never calls an LLM. | none |
| `UI_AUTOMATION_DISCOVERY_MODEL` | the model | `gemini-3.8-flash` |
| `UI_AUTOMATION_FALLBACK_MODELS` | tried in order when the model is overloaded or out of quota | `gemini-3.5-flash,gemini-3-flash-preview` |
| `UI_AUTOMATION_OPERATOR_PORT`, `UI_AUTOMATION_BANK_PORT` | ports of the web app and the mock bank | `8001`, `8000` |
| `MOCK_BANK_USER`, `MOCK_BANK_PASSWORD` | the mock bank's teller sign-on, read through secret references | the bank's synthetic account |

Timeouts and polling intervals are named in [settings.py](src/ui_automation/settings.py).

## Evidence and tests

```bash
python scripts/generate_evidence.py             # rebuild evidence/ (Gemini for discovery if a key is set)
python scripts/generate_evidence.py --offline   # scripted discovery decisions, labelled as such
python scripts/generate_evidence.py --only 1-discovery   # redo some folders, keep the rest
pytest                                          # unit tests + end-to-end runs in a real headless browser
pytest -m "not integration"                     # unit tests only (under a second)
ruff check .
```

The end-to-end tests cover every result status, every recovery, the commit-point rules, drift on a new app version, tickets resolved by a simulated operator on the live session, staff take-over and stop, multi-run validation, an agent invoking a capability over HTTP, replay on the pixel surface at two window sizes, and a privacy test that runs a full discovery and checks that no member data reaches the model or any file. The pixel tests are skipped if the `pixel` extra is not installed.

## Layout

```
src/ui_automation/
  command_line.py         the ui-automation commands
  settings.py             paths, ports, timeouts (no magic numbers elsewhere)
  config_files.py         reads config/ (app profiles, safety policy)
  capability_workflows.py learn a capability; run a capability
  evidence_writer.py      the masked per-run event log and files
  models/                 capability recipe, app profile, result contract (Pydantic)
  screen/                 the Screen interface; the web adapter (DOM, page_inspector.js);
                          the pixel adapter (screenshots + OCR, mouse and keys; no DOM)
  discovery/              discovery agent, LLM clients, llm_privacy, locator candidates, recipe builder
  replay/                 replay runner, known-screen detection, sign-on
  catalog/                capability store, request router, review (card, approve, reject),
                          agent tools (the tool catalog and JSON Schemas)
  safety/                 safety policy (allowlist, risk classes), log masking
  human_handoff/          tickets, control lease, ticket instructions, operator recorder
  web/                    requests page and control room: API, job runner, templates, static
mock_bank/                the target app, sample members, fault injection
config/                   safety_policy.yaml and apps/acmecore_teller.yaml (the app profile)
capabilities/             approved capabilities: <app>/<name>/<version>.yaml
schemas/                  JSON Schemas generated from the models (ui-automation schemas)
scripted_discovery/       offline decision scripts for the demo requests
scripts/generate_evidence.py
tests/unit, tests/end_to_end
evidence/                 generated by scripts/generate_evidence.py
```
