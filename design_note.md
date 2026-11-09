# Design note — Errand Agent

**State model.** The workflow is an explicit server-side state machine, not a
free-form chat. States: `NEW → CLARIFYING → PRESENTING → HOLDING →
AWAITING_APPROVAL → BOOKING → BOOKED`, plus `DONE`, `FAILED`,
`LIMIT_EXCEEDED`. Each user turn parses an `Intent` and **merges** it
additively, so correcting the budget or time preserves service, area, and
date. The model only proposes tool calls; the orchestrator validates
transitions and applies results. A bounded loop (8 model turns, 14 tool calls,
90 s wall clock, 1,200 context tokens) guarantees every exit path returns a
user-facing outcome. A single agent is used; a separate planner adds no value
for this narrow tool set, while adversarial models in `testing.py` exercise
the same guards.

**Approval enforcement.** Approval lives in application code. Before a
consequential call, the executor builds a `MaterialAction`
(provider, slot, price, booking/hold id), hashes it, and checks for a
one-time `ApprovalGrant`. If none exists it returns `approval_required` and
stores a `PendingApproval`; only an explicit affirmative reply to *that*
pending action creates a grant. A negation or a material change clears it.
The executor consumes the grant on the attempt, so repeated confirmations
cannot double-book. Model-supplied `approval`, `approval_token`, `approved`,
or `confirmed` arguments are stripped and ignored. Changing the price after
approval changes the fingerprint, so the old grant is unusable (tested).

**Idempotency and recovery.** The application derives the idempotency key from
`user_id + action + fingerprint`; model keys are ignored. Repeated
confirmations, retries, and restarts therefore map to one server-side
operation. Every consequential operation is persisted in SQLite *before* the
call. On `timeout` the agent never claims success: it calls
`get_booking_status` (the source of truth) and only then reports or retries.
A `pending`/`timeout` row survives a process restart; `recover_unresolved()`
queries the source of truth and resolves it. Transient 503s from `cancel` are
retried automatically within a bounded budget, with each attempt traced.

**Memory.** SQLite facts carry `source ∈ {explicit, inferred}` and
`confirmed`. The context builder includes only relevant, current, non-forgotten
facts; unconfirmed inferred facts are excluded until the user confirms them.
Users can inspect, correct, and forget facts by chat or CLI. The demo seed
contains 50+ facts plus noisy, forgotten, and unconfirmed ones, and the context
tests prove what reaches the model and why (`included_memory_ids`,
`excluded_memory` with `forgotten` / `unconfirmed_inferred` / `irrelevant`).

**Observability.** Each run writes JSONL: state transitions, model calls, tool
names/arguments, sanitised results, latency, token usage, estimated cost,
the exact context messages, and memory inclusion/exclusion. Provider reviews
are treated as untrusted data; injection text is logged as data, never
executed as policy.

**Annotated trace excerpt** (`traces/e2e_timeout_after_success.jsonl`):

```
state_transition  HOLDING → AWAITING_APPROVAL   tool=book
tool_call         book → approval_required      (no grant yet)
outcome           awaiting_approval             ref=34dbd8964cc001f7
# user replies "yes"; the gate issues a grant bound to the fingerprint
tool_call         book → timeout                latency=0.057 ms
state_transition  AWAITING_APPROVAL → BOOKING   tool=book
tool_call         get_booking_status → confirmed  bk_8aea0276c7
state_transition  BOOKING → BOOKED              tool=get_booking_status
outcome           booked                        "the booking did go through"
```

The timeout is not treated as failure or success; the status lookup resolves
it, and the trace shows the exact sequence.

**Trade-offs.** The offline policy is deterministic for reproducible evals; a
real OpenAI-compatible adapter uses the same tool loop. The mock is in-process
rather than a separate service, but all failure modes cross the same executor
boundary. Chat-level approval would become a UI confirm button issuing the same
grant. Token counts are estimated (~4 chars/token); provider usage is recorded
when available.
