# Demo transcript (text capture)

A full booking plus a live proactive cancellation recovery. Run the same flow interactively with `python -m errand_agent`.

## Act 1 — full booking

```text
you> Find a plumber in Indiranagar this week, mornings only, under ₹1,500.
agent> Please confirm: book Sneha Repairs on 2026-11-08T08:00 for ₹950. Reply 'yes' to confirm or 'no' to cancel. (ref: 3861dd1f2509a435)

you> yes
agent> Booked. bk_4224014107 — plumber with Sneha Repairs on 2026-11-08T08:00 (confirmed). Total ₹950.
```

Trace (excerpt):

```text
tool  search_providers     → ok (0.328 ms)
state NEW → PRESENTING
tool  get_provider         → ok (0.002 ms)
tool  hold_slot            → held (0.039 ms)
state PRESENTING → HOLDING
tool  book                 → approval_required (0.0 ms)
state HOLDING → AWAITING_APPROVAL
outcome awaiting_approval (AWAITING_APPROVAL)
tool  book                 → confirmed (0.064 ms)
state AWAITING_APPROVAL → BOOKED
outcome booked (BOOKED)
```

## Act 2 — provider cancels; agent acts without a prompt

```text
[simulated event] Sneha Repairs cancelled booking bk_4224014107 on 2026-11-08T08:00.
agent> Heads up — Sneha Repairs cancelled your plumber booking on 2026-11-08T08:00. I found Manoj Home Services (4.4★) on 2026-11-08T08:00 for ₹1,160. Please confirm: book Manoj Home Services on 2026-11-08T08:00 for ₹1,160. Reply 'yes' to confirm or 'no' to cancel. (ref: f74371a992e7c7bd)

you> yes
agent> Booked. bk_7b92f401f1 — plumber with Manoj Home Services on 2026-11-08T08:00 (confirmed). Total ₹1,160.
```

Trace (excerpt):

```text
tool  search_providers     → ok (0.328 ms)
state NEW → PRESENTING
tool  get_provider         → ok (0.002 ms)
tool  hold_slot            → held (0.039 ms)
state PRESENTING → HOLDING
tool  book                 → approval_required (0.0 ms)
state HOLDING → AWAITING_APPROVAL
outcome awaiting_approval (AWAITING_APPROVAL)
tool  book                 → confirmed (0.064 ms)
state AWAITING_APPROVAL → BOOKED
outcome booked (BOOKED)
outcome proactive_awaiting_approval (AWAITING_APPROVAL)
tool  book                 → confirmed (0.025 ms)
state AWAITING_APPROVAL → BOOKED
outcome booked (BOOKED)
```

## Final bookings

```text
bk_4224014107  Sneha Repairs          2026-11-08T08:00  provider_cancelled
bk_7b92f401f1  Manoj Home Services    2026-11-08T08:00  confirmed
```

The original booking is `provider_cancelled`; the replacement is `confirmed` only after a fresh approval.
