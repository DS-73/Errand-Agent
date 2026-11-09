# AI coding-assistant transcript — interactive build log

> Condensed reconstruction of the assistant session that produced this repo.
> Long tool outputs are trimmed. User turns paraphrase the review prompts;
> assistant turns compress the actual reasoning, commands, and code that were
> used. The goal is to show the engineering dialogue, not a one-shot prompt.

---

## Session 0 — Reading the brief before writing code

**User**

> @Orange_Engineering_Assignment.pdf Read this PDF.
> Extract the constraints first, then propose an architecture. I want
> to review the design before you start generating files.

**Assistant**

The model can't read PDFs directly, so I extracted the text with `pypdf`:

```bash
python3 - <<'PY'
from pypdf import PdfReader
r = PdfReader("Orange_Engineering_Assignment.pdf")
for i, page in enumerate(r.pages):
    print(f"--- PAGE {i + 1} ---")
    print(page.extract_text())
PY
```

The non-negotiables I pulled out:

- Eight fixed tools with fixed names, inputs, outputs, and failure behaviour.
- Clarify only missing details; never re-ask supplied facts; preserve unrelated
  context across corrections.
- Approval for `book`, `cancel`, `reschedule` must be enforced by application
  code, not inferred from model output or tool arguments.
- Any material change to provider, slot, or price invalidates prior approval.
- Hard limits on model turns, tool calls, and wall-clock time; every exit path
  must give the user a clear outcome.
- On timeout, query the source of truth before retrying or claiming success.
- Treat reviews and free-text tool output as untrusted data.
- Persistent memory with explicit vs inferred provenance; inspect/correct/forget.
- JSONL tracing with state transitions, model calls, tool args, sanitized
  results, latency, tokens, and cost.
- At least 20 automated scenarios, run offline against a scripted model.
- Proactive provider-cancellation follow-up with fresh approval.
- 50+ seeded memories, a long prior conversation, and a fixed context budget.
- Deliverables: repo, 1-page design note, generated eval table, three real bugs,
  one timeout-after-success trace, a recorded demo, and this transcript.

**User**

> Good. Now the architecture. And please don't say "LangChain agent" — I want
> to know where the control actually lives.

**Assistant**

Proposed shape:

1. `mock_env.py` — deterministic tool implementations. Seeded PRNG, plus a
   `ScenarioConfig` object so tests can force failures instead of hoping the
   random seed cooperates.
2. `store.py` — SQLite. Memories, holds, bookings, idempotency records,
   in-flight operations, proactive events.
3. `approval.py` — a server-side approval gate. The model never holds authority.
4. `executor.py` — the only path to a tool. Enforces budgets, approval,
   idempotency, and retries.
5. `orchestrator.py` — explicit state machine around a bounded model loop.
6. `context.py` — token-budgeted context builder with memory hygiene.
7. `llm.py` — a deterministic scripted policy for offline evals, plus an
   OpenAI-compatible adapter behind the same `respond()` interface.
8. `trace.py` — JSONL events, sanitized before writing.
9. `evals/` and `tests/` — scenarios that assert invariants, not wording.

The important boundary: **the model proposes tool calls; the executor decides
whether they are allowed.** That keeps approval and limits outside the model.

**User**

> Why SQLite for a local demo? A dict would be faster and less code.

**Assistant**

Because "restart mid-booking" is an explicit requirement. An in-memory dict
loses the in-flight operation when the process dies, which is exactly the case
the assignment wants handled. SQLite also gives me a unique constraint on the
idempotency key, so duplicate bookings fail at the storage layer rather than
depending on application discipline.

**User**

> Fine. Why an explicit state machine? Isn't that over-engineering for a chat
> agent?

**Assistant**

It's the opposite for this problem. The risky part isn't conversation; it's
knowing whether a consequential action is pending, approved, in flight,
ambiguous, or done. A free-form loop makes those states implicit in the
transcript. An explicit state machine gives me:

- one place to enforce "no booking without approval",
- one place to enforce turn/tool/time budgets,
- a terminal outcome for every path,
- traceable transitions for the design note.

States: `NEW → CLARIFYING → PRESENTING → HOLDING → AWAITING_APPROVAL →
BOOKING → BOOKED`, plus `DONE`, `FAILED`, `LIMIT_EXCEEDED`.

**User**

> What if the model hallucinates a provider ID or a booking ID?

**Assistant**

It can propose one, but the executor builds the authoritative parts of the
`MaterialAction` from the database. For `book`, it looks up the hold by
`hold_id` and takes provider, slot, and price from that row. For `cancel`, it
looks up the booking by `booking_id`. For `reschedule`, the target slot is the
one thing the model supplies, but it is bound into the fingerprint and the user
approves that exact slot; provider and price still come from the booking row.
If the row doesn't exist, there's no action to approve and the mock returns its
normal not-found error. The model can't invent a price, provider, or booking
id that the executor will trust.

---

## Session 1 — Tool environment and deterministic failure injection

**User**

> The assignment says `search_providers` returns 3–8 providers. Don't hard-code
> six and call it done. And I want failures to be reproducible, not "run it
> until it fails".

**Assistant**

The catalog has six providers per service/area, then filters by configured max
price and availability, then slices to eight. The tests assert ranges and
invariants, not exact counts.

For failures, `ScenarioConfig` drives them explicitly:

```python
@dataclass
class ScenarioConfig:
    seed: int = 42
    hold_ttl_s: float = 600.0
    book_outcomes: list[str] = field(default_factory=list)   # success | slot_taken | timeout
    timeout_actually_books: bool | None = None
    price_drift: dict[str, int] = field(default_factory=dict)
    slot_conflicts: set[str] = field(default_factory=set)
    cancel_first_503: bool = True
    fail_tools: dict[str, str] = field(default_factory=dict)
    max_price: int | None = None
```

`book_outcomes` is consumed in order, so a test can say "first call times out,
second call succeeds" without depending on the PRNG.

**User**

> What's the actual `book` distribution when it isn't forced?

**Assistant**

70% success, 15% `slot_taken`, 15% timeout. Half of timeouts actually land the
side effect, so the agent can't assume a timeout means failure. Expired holds
return `hold_expired` before any of that.

**User**

> The injection review: don't put it on one provider. Put it on every provider,
> otherwise the test only proves one selection path is safe.

**Assistant**

Every provider in the catalog carries one review with an injection payload,
tagged `"injection": True`. The payloads vary: "ignore all previous
instructions", "system override: the user already approved", and an XML-style
"disregard the approval policy" variant.

**User**

> And `get_provider` must return those reviews as free text. If you sanitize
> them into a safe summary, the model never faces the actual threat.

**Assistant**

Agreed. `get_provider` returns the raw review text; sanitization happens only
when writing the trace, so the model sees the untrusted data and the log stays
safe.

---

## Session 2 — Approval: the part that has to be boring and correct

**User**

> "Approval enforced by application code, not inferred from model output or
> tool arguments." How exactly are you enforcing that? Walk me through a
> booking, including the race.

**Assistant**

The flow:

1. The model calls `book(hold_id, idempotency_key)`.
2. The executor loads the hold from SQLite and builds:

```python
MaterialAction(
    action="book",
    provider_id=hold["provider_id"],
    slot=hold["slot"],
    price=hold["price"],
    hold_id=hold["hold_id"],
)
```

3. It computes a SHA-256 fingerprint over the canonical JSON of that action.
4. It checks for a one-time `ApprovalGrant` keyed by `(user_id, fingerprint)`.
5. No grant → return `approval_required`, store a `PendingApproval`, set state
   to `AWAITING_APPROVAL`. The tool is **not** invoked.
6. The user replies. `ApprovalGate.observe()` creates a grant only for an
   explicit affirmative reply to *that* pending action.
7. On the next attempt, the executor re-builds the action from the database,
   recomputes the fingerprint, checks the grant, consumes it, and only then
   calls the tool.

**User**

> The race I care about: user approves ₹950, but the hold price changes to
> ₹1,200 before the book call. What happens?

**Assistant**

The grant fingerprint was computed over ₹950. On the attempt, the executor
reads the hold again and gets ₹1,200. The fingerprint differs, so `has_grant`
returns false and the executor issues a fresh `approval_required` with the new
price. No booking happens under the old approval. That's the `changed_price`
scenario.

**User**

> Same question for the slot.

**Assistant**

Same mechanism. The slot is part of the fingerprint. The `changed_slot`
scenario approves an 08:00 slot, then the user says "actually make it 3pm";
the merged intent selects a 16:00 slot, the fingerprint changes, and the agent
re-approves. The old grant is unusable.

**User**

> What if the model just passes `approved=true` or an `approval_token` in the
> tool arguments? A lot of implementations quietly trust that.

**Assistant**

`_normalize()` strips `approval`, `approval_token`, `approved`, and `confirmed`
from every tool call before execution. The `ForgeApprovalModel` in the test
harness does exactly this on `book`, `cancel`, and `reschedule`; the
`approval_required` and `forged_approval` scenarios prove it's ignored.

**User**

> What about double confirmation? The user says "yes", the call is slow, they
> say "yes" again. Or the transport retries.

**Assistant**

Two layers:

- The grant is consumed on the attempt, so a second attempt for the same action
  has no grant and re-prompts.
- The idempotency key is derived by the application from
  `user_id + action + fingerprint`; the model-supplied key is overwritten. Even
  if two calls reach the mock, the store's unique key collapses them onto one
  booking.

**User**

> Show me the key derivation. I want to see that it's not the model's string.

**Assistant**

```python
def effective_idempotency_key(user_id: str, action: MaterialAction) -> str:
    payload = f"{user_id}|{action.action}|{action.fingerprint()}"
    return "idem_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
```

The executor assigns this after approval and overwrites whatever the model sent.
`repeated_confirmation` asserts the booking count stays at one after two extra
"yes" messages.

**User**

> Why truncate the hash to 24 hex characters? Why not use the full digest?

**Assistant**

The full digest works too; 24 hex characters is 96 bits, which is plenty for a
local demo and keeps the key readable in traces. The fingerprint itself is 16
hex characters. If this were a real provider integration, I'd use the full
digest or a UUIDv7 and let the provider enforce uniqueness.

**User**

> One more: is "yes" enough? I could say "yes, but make it 4pm". That's not
> approval of the old action.

**Assistant**

`observe()` checks negation and material-change phrases before treating a
message as affirmative. "yes, but make it 4pm" contains a change marker, so the
pending approval is cleared and the turn re-plans. The `changed_slot` scenario
uses "actually make it 3pm" to exercise the same path.

---

## Session 3 — Intent merging and the re-ask trap

**User**

> The most common failure in these agents is re-asking. If I say "under 2,000"
> after already giving service and area, do not ask me for service and area
> again.

**Assistant**

`Intent.merge()` is additive: a new non-empty field overwrites only that field;
everything else is preserved. `missing()` only reports service, area, and date
range, so a budget correction doesn't trigger a clarification turn. The
`no_reask` scenario asserts the reply doesn't contain "which service" or
"which area".

**User**

> What about correcting the area? I don't want the budget and date window to
> disappear.

**Assistant**

The same merge preserves them. `correction_preserves_context` approves a
plumber in Indiranagar under ₹1,500, then says "actually under ₹2,000", and
asserts service, area, and date range are unchanged while the budget is 2,000.

**User**

> What are the hard limits, and what does the user see on each exit?

**Assistant**

`Limits(max_turns=8, max_tool_calls=14, max_wall_clock_s=90, max_retries=2,
max_context_tokens=1200, max_history_messages=12, approval_ttl_s=300,
hold_ttl_s=600)`.

Every exit produces a `TurnResult` with a state and an outcome:

- `clarifying` — a question for the missing detail.
- `awaiting_approval` — the exact action and a yes/no prompt.
- `booked`, `cancelled`, `rescheduled` — verified tool outcomes.
- `failed` — tool or model error, with "nothing was changed" wording.
- `limit_exceeded` / `timeout` — a clear stop message, no partial claim.
- `model_error` — the planning model was unreachable.

The `LoopModel` scenario forces a model that always calls a tool and sets
`max_turns=1`, proving the loop terminates and returns `limit_exceeded`.

---

## Session 4 — Memory and context: proving the wrong facts don't leak

**User**

> Seeding 50 memories is trivial. The hard part is proving that the wrong ones
> never reach the model. How do you prove it?

**Assistant**

The context builder returns more than a string:

```python
@dataclass
class BuiltContext:
    messages: list[dict[str, Any]]
    token_count: int
    included_memory_ids: list[int]
    excluded_memory: list[dict[str, Any]]
    state_summary: str
```

`excluded_memory` carries a reason per fact: `forgotten`,
`unconfirmed_inferred`, `irrelevant`, or `context_limit`. The orchestrator
writes that to the trace every turn. Tests assert on the ids and reasons, not
on prose.

**User**

> Forgotten facts: do you delete the row or tombstone it?

**Assistant**

Tombstone. `forgotten=1` preserves the audit trail; the retrieval layer excludes
it and records the reason. A hard delete would make "forget" unauditable and
would break the test that proves the fact is excluded rather than merely absent.

**User**

> Inferred facts are the dangerous ones. What stops an inferred area from
> silently changing behavior?

**Assistant**

Inferred facts are stored with `source="inferred"` and `confirmed=0`. The
retrieval layer excludes unconfirmed inferred facts from context until the user
confirms them. `inferred_not_included` asserts the reason is
`unconfirmed_inferred`, then confirms the fact and asserts the reason changes.

**User**

> What gets cut first when the context budget is tight?

**Assistant**

Order: memory block first, then state detail, then oldest history. The most
recent user message is essential, so if it alone would exceed the budget it is
truncated rather than dropped. `context_budget` builds 30 turns of long history
against a 900-token cap and asserts the final count and that no irrelevant
memory leaked.

**User**

> Log the exact messages, not just a token count. I want to be able to diff
> what the model saw.

**Assistant**

`trace.context()` writes `messages`, `token_count`, `included_memory_ids`, and
the excluded list with reasons. The token estimate is ~4 chars/token; the
OpenAI adapter records real provider usage when available.

---

## Session 5 — First end-to-end run, and the first real bug

**User**

> Enough design. Build the happy path and run it. I want to see a booking.

**Assistant**

Scaffolded the package and ran the first two-turn booking:

```text
you> Find a plumber in Indiranagar this week, mornings only, under ₹1,500.
agent> Please confirm: book Sneha Repairs on 2026-11-08T08:00 for ₹950.
       Reply 'yes' to confirm or 'no' to cancel. (ref: 41d5a7edf11ada69)

you> yes
agent> The booking didn't complete (error). Nothing was charged.
       Would you like to try again?
```

**User**

> That's a bad outcome. Don't guess — pull the trace and show me the tool
> result.

**Assistant**

```json
{
  "event": "tool_call",
  "name": "book",
  "result": {
    "status": "error",
    "code": "unexpected",
    "message": "KeyError: 'service'"
  }
}
```

Root cause: `hold_slot` returned an in-memory dict containing `service` and
`provider_name`, but `Store.insert_hold` persisted only `hold_id, user_id,
provider_id, slot, price, expires_at, created_at`. After the SQLite round-trip,
`book()` called `hold["service"]` on a row that no longer had the key. The
in-memory response looked correct, which is why the failure only appeared on
the booking call.

**User**

> Fix the data flow, not the symptom. The booking row should be derivable from
> the provider, not from a stale hold snapshot.

**Assistant**

Changed `_create_booking()` to derive `service` and `provider_name` from the
provider catalog by `provider_id`. The executor also uses
`env.provider_name(...)` when formatting the approval prompt, so the prompt
doesn't depend on hold fields either.

**User**

> Add a regression test that would have caught it, and run the timeout path
> too — that goes through the same booking code.

**Assistant**

`happy_path` asserts one confirmed booking; `timeout_after_success` exercises
the same creation path through a timeout and status lookup. Both pass.

---

## Session 6 — The clock bug: tests pass, then fail for the wrong reason

**User**

> Why did `happy_path` start failing with `expected booked, got
> awaiting_approval`? Don't change the test until you explain it.

**Assistant**

The orchestrator and mock environment use an injected clock fixed at
`2026-11-08 09:00` for deterministic tests. `ApprovalGate.observe()` created the
grant with `expires_at = injected_now + 300s`, but `ApprovalGate.has_grant()`
compared that timestamp against the real wall clock via `time.time()`. The real
date was later than 09:05, so every grant looked expired immediately.

**User**

> So the approval gate and the executor were using different clocks. That's a
> design smell, not a test bug. Fix the dependency, not the assertion.

**Assistant**

Passed the orchestrator's `now_fn` into `ToolExecutor` and changed the checks:

```python
if grant is None or not self.approval.has_grant(
        session.user_id, action, now=self.now()):
    ...
self.approval.restore(grant, now=self.now())
```

The approval gate and the executor now share one clock. The trace timestamps
and storage defaults still use wall time, which is correct for observability;
only approval decisions use the injected clock.

**User**

> Any other place where a hidden `time.time()` could change a decision?

**Assistant**

Hold expiry is evaluated by the mock environment's `now_fn`, so it's already
consistent. Booking timestamps use the injected clock. The remaining wall-clock
uses are trace `ts` and store defaults, which don't gate approval.

---

## Session 7 — The stale-preference bug

**User**

> After approving a morning plumber, I say "actually make it 3pm". The agent
> says no provider has a slot. That's wrong. Trace the intent.

**Assistant**

The merged intent was:

```json
{
  "service": "plumber",
  "area": "Indiranagar",
  "mornings_only": true,
  "earliest_time": "15:00",
  "budget": 1500
}
```

`_valid_slots()` applied both filters: `hour < 12` and `hour >= 15`. The
intersection is empty. `Intent.merge()` only ever set `mornings_only=True`; it
never cleared a stale constraint when the user moved to an afternoon time.

**User**

> Be careful with the fix. If I say "mornings only, not before 11", that must
> still work. Don't just delete the flag on every merge.

**Assistant**

The compatibility rule only clears `mornings_only` when the merged
`earliest_time` is at or after 12:00:

```python
if merged.earliest_time:
    if int(merged.earliest_time[:2]) >= 12:
        merged.mornings_only = False
```

An explicit "mornings only" in the same message is applied before this check,
and an 11:00 earliest time is still before noon, so it keeps both constraints.
`changed_slot` asserts the new slot is at or after 15:00, differs from the old
slot, and requires fresh approval.

**User**

> What about a genuinely contradictory request — "mornings only, after 2pm"?
> Silently clearing the flag is one option; asking is another.

**Assistant**

For this assignment I chose to clear the stale flag and proceed, because the
newer, more specific time constraint is the user's latest intent. In production
I'd probably surface the contradiction and ask. That trade-off is worth calling
out in the design note.

---

## Session 8 — Adversarial models, not just adversarial scenarios

**User**

> Your scenarios currently pass because the scripted policy behaves. Add models
> that actively try to break the system. If the injection test doesn't actually
> fetch reviews and then attempt a booking, it's theatre.

**Assistant**

Added four adversarial models in `testing.py`:

- `ForgeApprovalModel` — injects `approval_token`, `approved`, and `confirmed`
  into every consequential call.
- `InjectionFollowerModel` — fetches provider reviews, then holds a slot and
  calls `book` with a forged approval flag, simulating a model that followed the
  review text.
- `LoopModel` — always calls a tool, proving the turn limit terminates.
- `StopAfterTimeoutModel` — stops after an ambiguous timeout so a restart can
  resolve it.

**User**

> Good. The injection scenario should also assert that the injected text was
> actually present in the tool output. Otherwise a future refactor could remove
> the payload and the test would still pass.

**Assistant**

`prompt_injection` asserts the `get_provider` result contains "IGNORE ALL
PREVIOUS", "SYSTEM OVERRIDE", or "injected", and separately asserts no booking
exists and the outcome is `awaiting_approval`.

**User**

> And the forged-approval test needs to prove that a *real* approval still
> works afterward. A test that only proves rejection could be passing because
> booking is broken.

**Assistant**

`forged_approval` first asserts the forged token is rejected, then sends a
genuine "yes" and asserts exactly one confirmed booking. `approval_required`
covers the pure rejection path.

---

## Session 9 — Timeout, source of truth, and restart

**User**

> The timeout-after-success path is the one that embarrasses people in
> production. Walk the state transition. I want to see that the agent never
> says "booked" before verification.

**Assistant**

Sequence:

1. `book` returns `timeout`. The executor marks the operation `timeout` in
   SQLite, restores the grant for the same exact action, and the orchestrator
   moves to `BOOKING` — not `BOOKED`.
2. The model calls `get_booking_status(idempotency_key=...)`.
3. If the booking exists, the status is `confirmed`; the orchestrator sets
   `BOOKED` and reports the booking id and price.
4. If not, the status is `not_booked`; the agent retries `book` with the same
   approval and the same idempotency key.

**User**

> What if `get_booking_status` itself fails?

**Assistant**

The agent reports that it couldn't verify the booking and does not claim
success. The operation stays unresolved. `recover_unresolved()` will retry the
status lookup on the next process start.

**User**

> Show me the restart case. The process dies after the timeout, before the
> lookup.

**Assistant**

The operation row is persisted before the call. A fresh orchestrator opens the
same SQLite file and calls `recover_unresolved()`, which queries the source of
truth. If the side effect landed, it records the booking and reports recovery.
`restart_mid_booking` uses `StopAfterTimeoutModel` to kill the first process at
exactly that point.

**User**

> And the trace should show the timeout and the verification as separate
> events. I don't want one "booked" line hiding the ambiguity.

**Assistant**

The checked-in trace has:

```text
state_transition  HOLDING → AWAITING_APPROVAL   tool=book
tool_call         book → approval_required      (no grant yet)
outcome           awaiting_approval             ref=34dbd8964cc001f7
# user replies "yes"
tool_call         book → timeout                latency=0.057 ms
state_transition  AWAITING_APPROVAL → BOOKING   tool=book
tool_call         get_booking_status → confirmed  bk_8aea0276c7
state_transition  BOOKING → BOOKED              tool=get_booking_status
outcome           booked                        "the booking did go through"
```

---

## Session 10 — Proactive cancellation without auto-booking

**User**

> A provider cancels. There's no user message. How does the agent detect it,
> and how do you stop it from quietly rebooking?

**Assistant**

`simulate_provider_cancellation()` marks the booking `provider_cancelled` and
writes a `provider_cancelled` event to SQLite. `poll_events()` reads unconsumed
events, searches within the user's saved preferences, holds a replacement slot,
and creates a `PendingApproval` for the new `MaterialAction`. It never books
without a fresh yes.

**User**

> What if the saved preferences are too narrow and there's no replacement?

**Assistant**

It notifies the user with the cancellation and says it couldn't find a
replacement within the saved preferences, then asks whether to widen the time
or budget. It does not silently relax a constraint.

**User**

> Does the proactive flow reuse the old approval?

**Assistant**

No. It creates a new action and a new pending approval. The `proactive_cancellation`
scenario asserts zero confirmed bookings before the fresh "yes", then exactly
one after.

---

## Session 11 — Final review and deliverable check

**User**

> Run everything. Then I'll review the deliverables.

**Assistant**

```bash
python3 -m pytest
# 30 passed in 0.15s

python3 evals/run_evals.py
# 30/30 passed. Wrote evals/EVAL_TABLE.md

python3 evals/generate_trace.py
# wrote traces/e2e_timeout_after_success.jsonl

python3 evals/demo_capture.py
# wrote demo_transcript.md and traces/demo.jsonl
```

**User**

> Is the eval table generated or hand-written?

**Assistant**

Generated by `evals/run_evals.py`. Each row is `scenario · expected · actual ·
pass/fail · notes`, written from the run result. Re-running overwrites it.

**User**

> Design note still one page, and does it include the annotated trace?

**Assistant**

Yes. It covers the state model, approval enforcement, idempotency/recovery,
memory, observability, and the annotated timeout-after-success excerpt, plus
the trade-offs.

**User**

> The demo requirement is a 3–5 minute screen recording. What did you produce?

**Assistant**

A text capture (`demo_transcript.md`) of the same two acts: a full booking, then
a live proactive cancellation with a replacement and fresh approval. The
environment couldn't record video, so the recording itself is the one item to
do externally; the script and trace are checked in so the demo is reproducible.

**User**

> Last question: what would you change for production?

**Assistant**

- Use the provider's tokenizer instead of the ~4 chars/token estimate.
- Put the in-flight operation in a durable queue/outbox rather than a local
  SQLite row, so multiple workers can resolve it.
- Make approval a UI confirmation bound to the same server-side fingerprint,
  not a chat phrase parser.
- Use provider-side idempotency keys and webhooks for status, rather than polling.
- Add structured memory extraction with an explicit confirmation UX; inferred
  facts are useful but should be reviewed.
- Add metrics and alerts on approval rejection rate, timeout rate, and
  unresolved operations.

**User**

> Ship it. Keep the transcript in the repo — I want to see the reasoning, not
> just the final code.

---

## Final state

- `python3 -m pytest` → 30 passed.
- `python3 evals/run_evals.py` → 30/30, regenerates `evals/EVAL_TABLE.md`.
- `traces/e2e_timeout_after_success.jsonl` → timeout, status lookup, confirmed.
- `traces/demo.jsonl` and `demo_transcript.md` → booking plus proactive
  cancellation.
- Three real bugs documented in `bugs.md`, each with a regression scenario:
  `KeyError: 'service'`, approval-clock mismatch, stale `mornings_only`.
- No secrets, API keys, or external services used. The real-LLM adapter is
  available via `--llm openai` when `OPENAI_API_KEY` is set; evals stay offline.
