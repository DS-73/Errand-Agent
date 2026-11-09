# OrangeInvest Errand Agent

A web-based errand agent that arranges home-service errands through a FastAPI backend and React/Vite frontend interface. It clarifies only missing details, compares providers, holds slots, asks for explicit approval before consequential actions, and recovers safely from tool failures.

Everything runs locally with deterministic mocks. No real providers, payments,
or external integrations are required. The runtime is **Python 3.11+ stdlib
only** for the core agent; web interface requires FastAPI and Node.js.

---

## Quick start

```bash
# 1. Start the web interface (FastAPI + React/Vite)
make web                  # Install, build, then serve the UI via the API server
make web-api             # Run the FastAPI API server on http://127.0.0.1:8000
make web-ui              # Run the Vite dev server (proxies /api to the API server)

# 2. Run the 30-scenario eval suite and regenerate the table
make evals

# 3. Run the same scenarios under pytest
make test

# 4. Generate the end-to-end timeout-after-success trace
make trace
```

The web interface includes:
- Two-pane chat with approval cards
- Live trace viewer
- Memory inspector
- Booking management
- Server-side approval enforcement (cannot be forged by browser)

---

## What is implemented

### Tool environment (`errand_agent/mock_env.py`)

All eight required tools, with the required failure behaviours:

| Tool | Behaviour |
|---|---|
| `search_providers(service, area, date_range)` | 3–8 seeded providers with ids, prices, ratings, slots, attributes |
| `get_provider(provider_id)` | Details + free-text reviews; every provider carries a prompt-injection review |
| `hold_slot(provider_id, slot)` | 10-minute hold; quoted price can differ from the search price |
| `book(hold_id, idempotency_key)` | 70% success, 15% `slot_taken`, 15% timeout (half of timeouts actually booked); `hold_expired` past expiry |
| `get_booking_status(hold_id or idempotency_key)` | Authoritative source of truth for ambiguous outcomes |
| `cancel(booking_id, idempotency_key)` | First call returns a transient 503; the bounded retry succeeds |
| `reschedule(booking_id, slot, idempotency_key)` | `slot_conflict` on configured/occupied slots |
| `list_bookings(user_id)` | Past and upcoming bookings with id, provider, service, slot, status |

`ScenarioConfig` forces outcomes for reproducible tests (`book_outcomes`,
`timeout_actually_books`, `price_drift`, `slot_conflicts`, `fail_tools`, ...).

### Approval enforcement (`errand_agent/approval.py`, `executor.py`)

Approval is enforced by **application code**, not by trusting model output or
tool arguments:

* A `PendingApproval` is created from the exact `MaterialAction`
  (`provider_id`, `slot`, `price`, `booking_id`, `hold_id`).
* The gate grants only on an explicit affirmative reply to the pending action.
  A negation or a material change clears it.
* The grant is bound to a SHA-256 fingerprint of the material action and is
  one-time use. The executor consumes it on the attempt.
* A changed provider, slot, or price produces a different fingerprint, so the
  old grant cannot be used. Tests cover changed price and changed slot.
* Model-supplied `approval`, `approval_token`, `approved`, and `confirmed`
  arguments are stripped and ignored.

### Idempotency and recovery (`errand_agent/executor.py`, `store.py`)

* The idempotency key is **derived by the application** from
  `user_id + action + material fingerprint`; model-supplied keys are ignored.
  Repeated confirmations and network retries collapse onto one operation.
* Every consequential operation is persisted in SQLite before the call.
* A timeout is never reported as success. The agent calls
  `get_booking_status` and only then decides whether to retry or report.
* `recover_unresolved()` resolves `pending`/`timeout` operations after a process
  restart by querying the source of truth.
* The executor automatically retries a transient 503 (bounded by
  `Limits.max_retries`) and records each attempt in the trace.

### Workflow and context (`errand_agent/orchestrator.py`, `context.py`)

* Explicit states: `NEW → CLARIFYING → PRESENTING → HOLDING →
  AWAITING_APPROVAL → BOOKING → BOOKED`, plus `DONE`, `FAILED`,
  `LIMIT_EXCEEDED`.
* Additive intent merging: correcting one detail preserves unrelated context.
* Hard limits: model turns, tool calls, wall-clock time, context tokens.
  Every exit path produces a user-facing outcome.
* The context builder caps at 1,200 tokens (configurable), includes only
  relevant current preferences, and logs exactly what was sent, plus the
  memory ids and exclusion reasons (`forgotten`, `unconfirmed_inferred`,
  `irrelevant`, `context_limit`).
* Forgotten facts and unconfirmed inferred facts are never sent to the model.

### Memory (`errand_agent/memory.py`)

* SQLite-backed, persistent across sessions/processes.
* Distinguishes `explicit` from `inferred`; inferred facts are unconfirmed and
  excluded from context until confirmed.
* Users can inspect, correct, and forget facts via chat or CLI.
* `seed_demo_memories()` seeds 50+ facts, including noisy, forgotten, and
  unconfirmed-inferred ones for the context tests.

### Observability (`errand_agent/trace.py`)

JSONL trace per run with: state transitions, model calls, tool names and
arguments, sanitised results, latency, token usage, estimated cost, context
messages, included/excluded memory, and the final outcome. Tool output is
sanitised (control characters, secrets, length) before logging.

An end-to-end trace for a timeout-after-success run is checked in at
`traces/e2e_timeout_after_success.jsonl`.

### Proactive follow-up

`MockErrandEnvironment.simulate_provider_cancellation()` marks a booking
`provider_cancelled` and enqueues an event. `ErrandOrchestrator.poll_events()`
detects it without a user prompt, searches for a replacement within stored
preferences, holds a slot, and asks for **fresh approval** before rebooking.
`/simulate-cancel <provider_id>` and `/events` expose this in the CLI.

---

## Project layout

```
errand_agent/
  approval.py       application-enforced approval gate
  context.py        token-budgeted context builder + memory hygiene
  executor.py       tool boundary: approval, idempotency, retries, budgets
  llm.py            scripted policy + OpenAI-compatible adapter + tool specs
  memory.py         explicit/inferred memory, correction, forgetting
  mock_env.py       deterministic mock tool environment
  models.py         dataclasses (Intent, MaterialAction, Limits, ...)
  nlu.py            offline intent parsing + approval phrases
  orchestrator.py   explicit workflow, bounded model loop, recovery, events
  store.py          SQLite persistence (memory, holds, bookings, ops, events)
  testing.py        harness + adversarial scripted models
  trace.py          sanitised JSONL tracing
  cli.py            interactive chat
evals/
  scenarios.py      30 automated scenarios
  run_evals.py      generates EVAL_TABLE.md from the run
  generate_trace.py generates the checked-in e2e trace
  EVAL_TABLE.md     generated, not hand-written
tests/
  test_scenarios.py pytest wrapper over the same 30 scenarios
traces/
  e2e_timeout_after_success.jsonl
design_note.md      1-page design note
bugs.md             three real bugs, fixes, regression tests
transcript.md       AI coding-assistant transcript / notes
demo_transcript.md  scripted 3–5 minute demo (text capture)
```

---

## Eval suite

30 scenarios, all passing, generated by `python evals/run_evals.py`:

```
python evals/run_evals.py      # 30/30 passed
python -m pytest -q            # 30 passed
```

Coverage includes: missing details, no re-asking, budget/time filtering,
prompt injection, forged approval, changed price/slot after approval,
repeated confirmation, timeout-after-success, timeout-not-booked, slot_taken,
hold_expired, cancel 503 retry, reschedule conflict, memory correction and
forgetting, unconfirmed inferred memory, restart mid-booking, proactive
cancellation, context budget, limit exhaustion, unexpected tool errors,
list bookings, "same technician as last time", "move AC servicing to Saturday",
and context-preserving corrections.

The generated table is `evals/EVAL_TABLE.md`.

---

## Deliverables map

| Assignment deliverable | Where |
|---|---|
| Git repo with implementation, mocks, tests, README | this repository |
| Design note (≤ 1 page) | `design_note.md` |
| Generated eval table (scenario · expected · actual · pass/fail · notes) | `evals/EVAL_TABLE.md` (written by `evals/run_evals.py`) |
| Three real bugs (what/why/fix/regression) | `bugs.md` |
| End-to-end trace with timeout-after-success (raw + annotated excerpt) | `traces/e2e_timeout_after_success.jsonl`, annotated in `design_note.md` |
| Recorded 3–5 min demo (chat + trace side by side) | `demo_transcript.md` is the text capture; a screen recording can follow the same two acts (`make demo` regenerates it) |
| AI coding-assistant transcript | `transcript.md` |

`make chat`, `make evals`, `make test`, `make trace`, `make demo`, `make web`,
`make web-api`, `make web-ui`, `make web-build`, and `make web-install` wrap
the commands above.

## Known limitations / trade-offs

* The default model is a deterministic scripted policy so evals are fully
  offline and reproducible. The OpenAI-compatible adapter uses the same
  tool-calling loop; set `--llm openai` to use it.
* The mock environment is in-process, not a separate service. The tool
  contracts and failure modes are still exercised through the executor boundary.
* Approval is a chat-level affirmative reply to a server-side pending action.
  In a UI you would replace the affirmative parser with a confirm button that
  issues the same `ApprovalGrant`; the executor check is unchanged.
* Token counts are estimated at ~4 chars/token; a production adapter should use
  the provider's tokenizer. The trace already records provider usage when
  available.

---

## Web interface (FastAPI + Vite/React)

The web layer is a thin FastAPI adapter over the same `ErrandOrchestrator`.
It runs locally on `http://127.0.0.1:8000` and never calls real providers,
payments, or external APIs. Approval stays server-side: the browser posts a
decision and the server verifies a pending approval exists before granting —
the client cannot forge an approval.

### Run

```bash
# dev: API server on :8000, Vite UI on :5173 (proxies /api to :8000)
make web-api          # terminal 1
make web-ui           # terminal 2

# one-shot: install deps, build the frontend, then serve from :8000
make web              # installs, builds, runs uvicorn

# or manually
python3 -m pip install -r requirements-web.txt
cd webapp/frontend && npm install && npm run build
python3 -m webapp --host 127.0.0.1 --port 8000
```

Then open `http://localhost:5173` (dev) or `http://localhost:8000` (built UI).

### Endpoints

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/api/health` | GET | none | health check |
| `/api/session` | GET | none | `token`, `user_id`, `model`, `limits`, `recovery` |
| `/api/message` | POST | `X-Web-Token` | send a chat message |
| `/api/approve` | POST | `X-Web-Token` | confirm/cancel a pending material action |
| `/api/events` | POST | `X-Web-Token` | poll proactive events (e.g. replacement booking) |
| `/api/trace` | GET | `X-Web-Token` | trace events filtered to the user's runs |
| `/api/memories` | GET | `X-Web-Token` | inspect stored facts with provenance |
| `/api/bookings` | GET | `X-Web-Token` | list the user's bookings |
| `/api/state` | GET | `X-Web-Token` | current session state |
| `/api/forget` | POST | `X-Web-Token` | forget a stored fact by key |
| `/api/simulate-cancel` | POST | `X-Web-Token` | simulate a provider cancellation |

A per-process token is returned by `/api/session`; every mutating route
requires `X-Web-Token`. CORS allows Vite ports `5173` and `4173`.

### Security notes

* The server binds to `127.0.0.1` only; it is not exposed to the network.
* The per-process `X-Web-Token` stops another local process from driving the
  agent through the API, but this is a localhost demo, not a production
  authentication system.
* Tool output is sanitised (control characters, secrets, length) before being
  written to trace or returned to the UI.

### Layout additions

```
webapp/
  backend/
    api.py       FastAPI routes, token auth, CORS, static/SPA serving
    service.py   WebAgent adapter: lock, trace filtering, payloads
  frontend/
    package.json, vite.config.ts, tsconfig.json, index.html
    src/
      main.tsx, App.tsx, api.ts, types.ts, styles.css
      components/ChatPane, ApprovalCard, TracePane, MemoryPanel
tests/
  test_web_api.py  TestClient tests for token auth, booking, trace, memory,
                   cancellation, events
```

