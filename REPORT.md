# Computer-Use Automation System: design report

## Architecture

The system is one Python process. Every action, whether it comes from the discovery agent or from a replay, passes through one **policy enforcement point** in the surface adapter, and every run writes redacted evidence.

- **Surface adapter** (`cua/surface/`). Playwright drives Chromium. One injected script, `dom.js`, both *observes* a screen and *resolves* recorded locators. Observing produces a numbered outline of every frame: roles, names, a derived label for unlabelled fields, and table header and row context. Because recording and replay share this one file, "the textbox in the same row as *Member Number*" means the same thing to both.
- **Discovery** (`agent.py`). Gemini gets the goal, the action history and the current outline. It answers with one tool call (`click`, `fill`, `select`, `extract`, `done`, `escalate`) that names an element number. The model never touches the browser.
- **Compiler and registry.** These turn the trace into a capability YAML file. The file is saved as a draft only after a replay in a fresh browser returns the same outputs.
- **Replay** (`replay.py`, `executor.py`). Deterministic steps, detectors for known screens, recoveries, and a typed result.
- **Session controller** (`session.py`). The control lease and the operator console, served from the same event loop.

Key decisions:

- **Element references, not pixel coordinates.** A click at (412, 233) does not replay safely. A click on *row where Member # = ${member_number} » link "View"* does. The outline uses the same signals as an accessibility tree, which desktop platforms also expose.
- **Locators are recorded while the element is live.** Candidates are generated at action time and kept only if each one matches exactly that element right then. They cannot be rebuilt reliably from logs afterwards.
- **Stateless per-step prompts.** Each prompt carries the goal, a short history and the current screen. Tokens stay bounded and every call is logged whole.
- **Files, not a database.** Capabilities diff and get reviewed like code, and versions are immutable. Production would use Postgres for the catalog and object storage for evidence.
- **A self-built hostile target** (framesets, label-less tables, random ids, JavaScript navigation) with faults on demand, which no public demo site offers.
- **Gemini Flash with a fallback chain.** The free tier has no Pro quota. Overload and quota errors retry, then switch model. When every model is exhausted, discovery stops cleanly with its evidence kept.

## Artifact schema

A capability is a **contract** plus a **flow** (`cua/schema.py`; examples in `evidence/capabilities/`).

The **contract** is what a calling agent sees, and it becomes the agent's tool definition:
- name, description, semver version, and status (`draft → approved`);
- `app`: the vendor product and version range it works on;
- typed `inputs`, each with a pattern and a sensitivity;
- typed `outputs` with a parse rule, where money is a `decimal` returned as an exact string;
- `risk`, and capability-specific business outcomes.

The **flow** is read only by the replay engine. Each step has an `intent` (the model's stated reason, kept for reviewers), an `action`, and a `target`. The target holds the frame, a **ranked list of locator strategies** and a fingerprint. A step may also have a `value` (a literal or a `${param}`), a `risk` (read, reversible or irreversible) and an `expect` postcondition. The flow ends with a `success` checkpoint, and **provenance** records the discovery run, model, validation run and approver.

Strategies are ordered from most to least semantic:
1. `role_name`: a control's role and accessible name.
2. `label_anchor`: a field found by the text beside it, for label-less table layouts.
3. `table_row` / `table_cell`: a row keyed by a column value, which may be a parameter.
4. `text`: the element's own text.
5. `css`: stable attributes only; random ids are never recorded.

None of them mentions Playwright. App-wide knowledge lives in a shared **app profile**: sign-on, the session-expired and maintenance screens, server errors, and PII formats. Validators reject a capability whose declared outputs are never extracted, whose step ids repeat, or whose risk is lower than its riskiest step.

The compiler makes the artifact general:
- Input example values become `${…}` references in values, locators, intents and titles.
- If a row was found by an input, other keys from that row are just the record's data, such as the member's name. They are dropped: they would match only this member and would put PII in the artifact. A literal key such as *Description = REGULAR SAVINGS* is a stable label and stays.
- Amounts inside dropdown labels are stripped, because they change between runs.
- A frame title that changes after a step becomes that step's postcondition.

## Determinism & error handling

**Finding elements.** Each step needs exactly one match; ambiguity is a failure, never a guess. The fingerprint confirms the match, and a fallback strategy still succeeds but records `drift`. **Waiting** uses no fixed sleeps: `wait_until` polls known screens, then the target, then the step timeout, and every action waits until no request is in flight and all frames have loaded.

**Result contract.** `status` is one of four values:

| Status | What the caller gets |
|---|---|
| `success` | the outputs |
| `business_outcome` | `code`, `message`, `step_id` |
| `failed` | `kind`, step, expected vs observed, `retryable`, screenshot and DOM snapshot |
| `escalated` | the unresolved intervention |

Recoveries, handoffs and drift are always listed. The replay exit codes are 0, 0, 1 and 3.

| Condition | Detected by | Class | Response |
|---|---|---|---|
| No records, access denied, validation error | screen text | business | returned as an answer |
| Maintenance interstitial | app profile | recoverable | dismiss, then retry the step |
| Session expired | app profile | recoverable | sign on again; restart only if nothing irreversible ran, else escalate |
| Slow page | postcondition not met yet | none (keep waiting) | wait up to the step timeout |
| Server error page | app profile | failed `APP_ERROR` | stop with screenshot, DOM snapshot and trace |
| Target missing or ambiguous | locator resolution | failed | expected vs observed |
| Identity code, unknown screen | app profile, or nothing matched | escalate | hand the live session to a person |

**Failing safely.**
- A screen that matches nothing known fails closed: it never "carries on".
- Once an irreversible step has acted, nothing is retried automatically.
- A known screen that appears *after* a step acted moves the run on instead of repeating the step. Repeating a Confirm would open a second account.

All 13 scenarios are recorded in `evidence/`.

**Limit:** discovery sees only the path it took. Business outcomes therefore come from the app profile and from review. Probe runs with known-bad inputs are the next step.

## Heterogeneity & multi-tenant

**Surfaces.** The seam is the adapter interface (`observe`, `resolve(target)`, `act`) plus the strategy vocabulary. Legacy web, with framesets, table layouts and JavaScript navigation, is the case already built.
- A **desktop adapter** would implement the same calls over UI Automation or AX: `role_name` becomes ControlType plus Name, `label_anchor` becomes LabeledBy or a spatial neighbour, and `table_row` becomes a DataGrid row.
- A **pixel adapter** (Citrix, terminals) would resolve strategies with OCR text anchors.

In both cases, flows, outcomes, the replay engine and the result contract stay as they are. A new surface adds a locator kind, not a new flow format.

**Tenants.** A capability belongs to a vendor product and version range, never to one institution. Tenant specifics already live apart, in the app profile: base URL, secret references, PII formats and branding. Flow differences would be small **overlays** (patches to strategies, labels, outcomes or steps). They resolve most specific first: tenant, then vendor version, then base. The result is hashed, and every run records the hash.

Drift shows up three ways: steps falling back to lower-ranked strategies (the `drift` records), a version banner outside the capability's range (read at sign-on), and checkpoint failures clustered by tenant and version.

The response is to mark the capability degraded for that tenant, re-discover in the tenant's test environment, and propose the diff from the base as an overlay. The overlay goes through the same validation and approval. Requests are always scoped to one tenant's app, so the set of candidate capabilities stays small even across hundreds of tenants.

## Escalation & handoff

**What counts as stuck:** discovery hits its step limit or timeout, or repeats an action on an unchanged screen; the model calls `escalate`; the policy holds back an irreversible step; replay meets a human-only or unrecognized screen; or a session expires after a commit.

**Control model.** One live session has one owner at a time. The lease is an `owner` plus an `epoch`: automation → nobody (paused) → `human:<operator>` → automation, with the epoch incremented on hand-back. The adapter checks the lease before *every* action, so automation cannot act while a person holds the session.

An intervention carries the capability, step, reason and a masked screenshot. It moves from `awaiting_human` to `human_control`, and then to `resumed`, `completed_by_human`, `aborted` or `timed_out`.

**Handing over.** The person works in the same headed browser, with the same cookies and the same page. Tools can also attach to that browser over a localhost CDP port; that is how the recorded demo operator works. A listener in every frame records the person's clicks and changes while they hold the lease. Typed values are kept only as their length.

**Handing back.** On resume, replay re-reads the screen and continues from the first step whose target resolves. In discovery, the model is told what the person did. A flow that a person partly performed is never compiled into a capability.

**Not built:** remote-browser streaming with input forwarding, an intervention queue with service levels, session keep-alive, and operator authentication.

## Safety

- **Allowlist, enforced twice.** First, a check before every action: the action type, and where its target navigates (`href` or the URL inside `onclick`). Second, a route filter on every network request, so a JavaScript button cannot reach a denied page such as "close membership".
- **Risky actions.** A click is classed irreversible when its control name contains a commit word (Confirm, Close, Transfer, Delete and so on). Typing counts as reversible only if the field submits by itself.
  - Discovery needs a person, or an explicit and logged `--allow-irreversible` in a test environment.
  - Replay needs an approved capability *and* `--allow-irreversible`; without both, the run is refused before it starts.
  - A validation replay stops before the commit point.

  In the evidence, only the approved run opened an account.
- **Prompt injection.** Page text is treated as data, and nothing the model says can bypass code-enforced policy. The agent ignored a planted "close this membership" note on member 31337, and the route is denied regardless.
- **Secrets and PII.** Artifacts hold `${secret:…}` references only. Redaction has four parts:
  1. registered values (sensitive inputs, extracted outputs, secrets) are masked wherever they appear;
  2. format patterns catch SSNs, card numbers, emails, account numbers and dollar amounts;
  3. screens and prompts are written to disk when the run closes, so values learned late are masked in earlier screens too;
  4. screenshots black out elements that show sensitive values.

  Tracing starts after sign-on, and replay keeps traces only on failure.
- **Limits.**
  - The risk classifier reads labels, so a commit button labelled "Save" would be misjudged. Step risk is visible in the artifact for review.
  - Names on screen that the system was never told about remain in discovery screens.
  - Traces hold raw DOM.
  - The console has no authentication; it is localhost only.
  - Discovery shows screens to a hosted model, so it belongs on test tenants with synthetic data. Replay sends nothing to any LLM.

## Cuts

**Deliberately minimal:**
- The operator console is a local page over a local headed browser.
- Only the web adapter exists; desktop, pixel and overlays are design only.
- Recorded-response cassettes became a scripted LLM client (`--script`), which also gives the offline path.

**Honest notes:**
- The Gemini free-tier quota ran out during the build. The irreversible `open_sub_account` discovery therefore used the scripted client and is labelled `model: scripted`. Everything except the model's decisions ran for real.
- Two early real-Gemini runs predate deferred redaction. They were re-scrubbed with the same redactor (`scripts/scrub_evidence.py`).

**Not built:** free-text routing (`cua ask`) over a capability catalog, compiling human actions into steps, bounded single-step LLM repair, and multi-run stability scores.

**Next, in order:**
1. A second branded variant of the mock bank, to make overlays and drift concrete.
2. Probe runs that learn business outcomes.
3. A capability catalog that agents can call as tools.
4. Policy-checked repair that proposes a new draft version.
5. Stability scoring to gate approval.
6. A remote-browser console with authentication.
