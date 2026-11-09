# Three real bugs hit while building

Each entry: what broke, why, the fix, and the regression test that now guards it.

---

## 1. `KeyError: 'service'` when booking after an approval

**What broke.** The first end-to-end booking reached the approval prompt, the
user said "yes", and the agent replied:

> The booking didn't complete (error). Nothing was charged. Would you like to try again?

The trace showed `book` returning `{"status":"error","code":"unexpected",
"message":"KeyError: 'service'"}`.

**Why.** `hold_slot` returned an in-memory dict containing `service` and
`provider_name`, but `Store.insert_hold` only persisted
`hold_id, user_id, provider_id, slot, price, expires_at, created_at`. After the
SQLite round-trip, `book()` called `hold["service"]` on a row that no longer
had that key. The in-memory response looked correct, which is why the bug only
appeared on the booking call.

**Fix.** `MockErrandEnvironment._create_booking` now derives `service` and
`provider_name` from the provider catalog by `provider_id`, and the executor
uses `env.provider_name(...)` when formatting the approval prompt. No schema
change was needed.

**Regression test.** `happy_path` (and every booking scenario) asserts a
confirmed booking; `timeout_after_success` exercises the same path through a
timeout and status lookup. Run:

```bash
python -m pytest -q tests/test_scenarios.py -k "happy_path or timeout_after_success"
```

---

## 2. Approval grants expired immediately under the test clock

**What broke.** After the user replied "yes", the agent re-surfaced the same
approval prompt instead of booking. `happy_path` failed with
`expected booked, got awaiting_approval`.

**Why.** The orchestrator and mock environment use an injected clock
(`2026-11-08 09:00`) for deterministic tests. `ApprovalGate.observe` created
the grant with `expires_at = injected_now + 300s`, but
`ApprovalGate.has_grant` compared that timestamp to the real wall clock
(`time.time()`), which was later than 09:05 on the same date. Every grant
therefore looked expired.

**Fix.** `ToolExecutor` now receives the same `now_fn` as the orchestrator and
calls `approval.has_grant(..., now=self.now())` and
`approval.restore(grant, now=self.now())`. The approval gate and the executor
share one clock.

**Regression test.** Every approval scenario, especially `happy_path`,
`approval_required`, and `forged_approval`. Run:

```bash
python -m pytest -q tests/test_scenarios.py -k "approval or happy_path"
```

---

## 3. Stale "mornings only" blocked a 3 p.m. correction

**What broke.** After approving a morning plumber, the user said
"actually make it 3pm". The agent replied that no provider had a free slot
instead of re-approving a 4 p.m. slot. `changed_slot` failed with
`expected re-approval, got info`.

**Why.** `Intent.merge` was deliberately additive, but it only ever *set*
`mornings_only=True`; it never cleared it. The merged intent ended up with
`mornings_only=True` **and** `earliest_time="15:00"`, so `_valid_slots`
required `hour < 12` and `hour >= 15` at the same time — an empty set.

**Fix.** After merging, if `earliest_time` is at or after 12:00, clear the
stale `mornings_only` constraint. An explicit "mornings only" in the same
message still wins because it is applied before the compatibility check.

**Regression test.** `changed_slot` asserts the new slot is at/after 15:00,
differs from the old slot, and requires fresh approval. Run:

```bash
python -m pytest -q tests/test_scenarios.py -k changed_slot
```

---

All three are also covered by the generated eval table
(`python evals/run_evals.py`, 30/30).
