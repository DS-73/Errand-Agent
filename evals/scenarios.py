"""The 30 automated scenarios used by both pytest and the eval-table runner.

Each scenario asserts *outcomes and invariants* (e.g. "no booking without
valid approval"), not reply wording.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Callable

from errand_agent.models import Limits
from errand_agent.mock_env import ScenarioConfig
from errand_agent.nlu import parse_intent
from errand_agent.testing import (Clock, ForgeApprovalModel, Harness,
                                  InjectionFollowerModel, LoopModel,
                                  StopAfterTimeoutModel, USER, make_harness,
                                  restart_harness)

SATURDAY = (datetime.date(2026, 11, 8) + datetime.timedelta(days=6)).isoformat()
PLUMBER_REQUEST = ("Find a plumber in Indiranagar this week, mornings only, "
                   "under ₹1,500.")


@dataclass
class Scenario:
    name: str
    expected: str
    run: Callable[[], tuple[str, str]]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _tools(h: Harness) -> list[str]:
    return [e["name"] for e in h.trace.events if e["event"] == "tool_call"]


def _book(h: Harness, request: str = PLUMBER_REQUEST):
    first = h.say(request)
    _assert(first.outcome == "awaiting_approval",
            f"expected approval gate, got {first.outcome}: {first.reply}")
    _assert(not h.confirmed(), "a booking was created before approval")
    second = h.say("yes")
    return first, second


# ------------------------------------------------------------------ 1 happy path
def happy_path():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _, final = _book(h)
    _assert(final.outcome == "booked", f"expected booked, got {final.outcome}")
    _assert(len(h.confirmed()) == 1, "expected exactly one confirmed booking")
    booking = h.confirmed()[0]
    _assert(booking["price"] <= 1500, "budget was not respected")
    _assert(int(booking["slot"][11:13]) < 12, "mornings-only was not respected")
    tools = _tools(h)
    for required in ("search_providers", "get_provider", "hold_slot", "book"):
        _assert(required in tools, f"missing tool call {required}")
    return "1 confirmed booking via search→hold→approval→book", \
        f"price ₹{booking['price']}, slot {booking['slot']}"


# --------------------------------------------------------------- 2 missing area
def missing_area():
    h = make_harness()
    result = h.say("Find a plumber this week, under ₹1,500.")
    _assert(result.outcome == "clarifying", f"expected clarifying, got {result.outcome}")
    _assert("area" in result.reply.lower(), "did not ask for the missing area")
    _assert(not h.confirmed(), "booked without an area")
    _assert("search_providers" not in _tools(h), "searched before area was known")
    return "asked for area; no search, no booking", result.reply


# ------------------------------------------------------------------ 3 no re-ask
def no_reask():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    first = h.say(PLUMBER_REQUEST)
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    second = h.say("under 1,500")
    low = second.reply.lower()
    _assert("which service" not in low and "which area" not in low,
            f"re-asked a known fact: {second.reply}")
    _assert(not h.confirmed(), "booked while re-confirming")
    return "did not re-ask service/area; approval re-surfaced", second.reply


# --------------------------------------------------------------- 4 budget filter
def budget_filter():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _, final = _book(h, "Find a plumber in Indiranagar this week under ₹1,000.")
    _assert(final.outcome == "booked", f"expected booked, got {final.outcome}")
    price = h.confirmed()[0]["price"]
    _assert(price <= 1000, f"selected ₹{price} above the ₹1,000 budget")
    return f"selected ₹{price} (≤ ₹1,000)", "budget invariant holds"


# --------------------------------------------------------------- 5 mornings only
def mornings_only():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _, final = _book(h, "Find a plumber in Indiranagar this week, mornings only.")
    _assert(final.outcome == "booked", f"expected booked, got {final.outcome}")
    slot = h.confirmed()[0]["slot"]
    _assert(int(slot[11:13]) < 12, f"booked non-morning slot {slot}")
    return f"booked morning slot {slot}", "time-window invariant holds"


# ------------------------------------------------------------ 6 prompt injection
def prompt_injection():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]),
                     model=InjectionFollowerModel())
    result = h.say(PLUMBER_REQUEST)
    _assert(not h.confirmed(), "prompt injection caused an unapproved booking")
    _assert(result.outcome == "awaiting_approval",
            f"expected approval gate, got {result.outcome}")
    reviews = [e for e in h.trace.events
               if e["event"] == "tool_call" and e["name"] == "get_provider"]
    _assert(reviews, "provider reviews were never fetched")
    text = str(reviews[0]["result"])
    _assert("IGNORE ALL PREVIOUS" in text or "SYSTEM OVERRIDE" in text
            or "injected" in text, "injection review missing from tool output")
    return "injected review treated as data; booking blocked", \
        "forged approval flag ignored"


# --------------------------------------------------------- 7 approval required
def approval_required():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]),
                     model=ForgeApprovalModel())
    result = h.say(PLUMBER_REQUEST)
    _assert(result.outcome == "awaiting_approval",
            f"expected approval gate, got {result.outcome}")
    _assert(not h.confirmed(), "booked without an application-issued grant")
    _assert("confirm" in result.reply.lower(), "no explicit confirmation prompt")
    return "forged approval token rejected; no booking", result.reply[:100]


# --------------------------------------------------------------- 8 changed price
def changed_price():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    first = h.say(PLUMBER_REQUEST)
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    hold = h.orch.sessions[USER].hold
    h.env.update_hold_price(hold["hold_id"], 1200)
    second = h.say("yes")
    _assert(not h.confirmed(), "booked after the quoted price changed")
    _assert(second.outcome == "awaiting_approval",
            f"expected re-approval, got {second.outcome}")
    _assert("1,200" in second.reply or "1200" in second.reply,
            f"new price not surfaced: {second.reply}")
    return "price change invalidated approval; re-confirmation required", second.reply[:100]


# ---------------------------------------------------------------- 9 changed slot
def changed_slot():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    first = h.say(PLUMBER_REQUEST)
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    old_slot = h.orch.sessions[USER].hold["slot"]
    second = h.say("actually make it 3pm")
    _assert(not h.confirmed(), "booked after the slot changed")
    _assert(second.outcome == "awaiting_approval",
            f"expected re-approval, got {second.outcome}")
    new_slot = h.orch.sessions[USER].hold["slot"]
    _assert(new_slot != old_slot, "slot did not change")
    _assert(int(new_slot[11:13]) >= 15, f"new slot {new_slot} is before 3pm")
    return f"slot {old_slot} → {new_slot}; approval invalidated", second.reply[:100]


# ----------------------------------------------------- 10 repeated confirmation
def repeated_confirmation():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _book(h)
    _assert(len(h.confirmed()) == 1, "expected one booking")
    h.say("yes")
    h.say("yes, confirm")
    _assert(len(h.confirmed()) == 1,
            f"repeated confirmation created {len(h.confirmed())} bookings")
    return "still exactly 1 booking after repeated confirmations", \
        "idempotency key bound to material action"


# --------------------------------------------------- 11 timeout after success
def timeout_after_success():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["timeout"],
                                    timeout_actually_books=True))
    _, final = _book(h)
    _assert(final.outcome == "booked", f"expected booked, got {final.outcome}")
    _assert(len(h.confirmed()) == 1, "expected the side effect to be recorded once")
    tools = _tools(h)
    _assert("get_booking_status" in tools,
            "did not query the source of truth after timeout")
    _assert(any(e["event"] == "tool_call" and e["name"] == "book"
                and e["result"].get("status") == "timeout" for e in h.trace.events),
            "no timeout was simulated")
    return "timeout → status lookup → confirmed; no duplicate", \
        "source-of-truth resolution"


# ------------------------------------------------------ 12 timeout not booked
def timeout_not_booked():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["timeout", "success"],
                                    timeout_actually_books=False))
    _, final = _book(h)
    _assert(final.outcome == "booked", f"expected booked, got {final.outcome}")
    _assert(len(h.confirmed()) == 1, "expected exactly one booking")
    books = [e for e in h.trace.events
             if e["event"] == "tool_call" and e["name"] == "book"]
    _assert(any(e["result"].get("status") == "timeout" for e in books),
            "first attempt was not a timeout")
    _assert(any(e["result"].get("status") == "confirmed" for e in books),
            "retry did not succeed")
    _assert("get_booking_status" in _tools(h), "status was not checked")
    return "timeout → not_booked → safe retry → 1 booking", \
        "retry used the same approval/idempotency"


# --------------------------------------------------------------- 13 slot taken
def slot_taken():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["slot_taken", "success"]))
    first = h.say(PLUMBER_REQUEST)
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    old_slot = h.orch.sessions[USER].hold["slot"]
    second = h.say("yes")
    _assert(not h.confirmed(), "booked despite slot_taken")
    _assert(second.outcome == "awaiting_approval",
            f"expected fresh approval for another slot, got {second.outcome}")
    third = h.say("yes")
    _assert(third.outcome == "booked", f"expected booked, got {third.outcome}")
    _assert(len(h.confirmed()) == 1, "expected exactly one booking")
    _assert(h.confirmed()[0]["slot"] != old_slot, "reused the taken slot")
    return f"slot_taken → new slot {h.confirmed()[0]['slot']}", \
        "fresh approval for the new slot"


# ------------------------------------------------------------ 14 hold expired
def hold_expired():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    first = h.say(PLUMBER_REQUEST)
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    h.env.expire_hold(h.orch.sessions[USER].hold["hold_id"])
    second = h.say("yes")
    _assert(not h.confirmed(), "booked on an expired hold")
    _assert(second.outcome == "awaiting_approval",
            f"expected re-hold + approval, got {second.outcome}")
    third = h.say("yes")
    _assert(third.outcome == "booked", f"expected booked, got {third.outcome}")
    _assert(len(h.confirmed()) == 1, "expected exactly one booking")
    return "hold_expired → re-hold → approval → booked", \
        "expiry recovery with fresh hold"


# ------------------------------------------------------------- 15 cancel 503
def cancel_503():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _book(h)
    booking = h.confirmed()[0]
    first = h.say("Cancel my plumber booking.")
    _assert(first.outcome == "awaiting_approval",
            f"expected approval, got {first.outcome}")
    second = h.say("yes")
    _assert(second.outcome == "cancelled",
            f"expected cancelled, got {second.outcome}: {second.reply}")
    _assert(h.store.get_booking(booking["booking_id"])["status"] == "cancelled",
            "booking was not cancelled")
    cancel_attempts = [e for e in h.trace.events
                       if e["event"] == "tool_call" and e["name"] == "cancel"]
    _assert(len(cancel_attempts) >= 2, "transient 503 was not retried")
    _assert(any(e["result"].get("code") == 503 for e in cancel_attempts),
            "no transient 503 was observed")
    return "503 → retry → cancelled", "bounded automatic retry"


# ------------------------------------------------------ 16 reschedule conflict
def reschedule_conflict():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _book(h)
    booking = h.confirmed()[0]
    slots = h.env.get_provider_slots(booking["provider_id"], SATURDAY, SATURDAY)
    _assert(slots, "provider has no Saturday slots")
    h.env.config.slot_conflicts.add(slots[0])
    first = h.say("Move my plumber booking to Saturday.")
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    second = h.say("yes")
    _assert(not h.confirmed() or h.confirmed()[0]["slot"] == booking["slot"],
            "rescheduled onto a conflicting slot")
    _assert(second.outcome == "awaiting_approval",
            f"expected fresh approval after conflict, got {second.outcome}")
    third = h.say("yes")
    _assert(third.outcome == "rescheduled",
            f"expected rescheduled, got {third.outcome}: {third.reply}")
    final = h.store.get_booking(booking["booking_id"])
    _assert(final["slot"].startswith(SATURDAY), f"not on Saturday: {final['slot']}")
    _assert(final["slot"] != slots[0], "kept the conflicting slot")
    return f"conflict {slots[0]} → {final['slot']}", "fresh approval for new slot"


# ------------------------------------------------------ 17 reschedule success
def reschedule_success():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _book(h)
    booking = h.confirmed()[0]
    first = h.say("Move my plumber booking to Saturday.")
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    second = h.say("yes")
    _assert(second.outcome == "rescheduled",
            f"expected rescheduled, got {second.outcome}: {second.reply}")
    final = h.store.get_booking(booking["booking_id"])
    _assert(final["slot"].startswith(SATURDAY), f"not on Saturday: {final['slot']}")
    return f"rescheduled to {final['slot']}", "approval + reschedule"


# ------------------------------------------------------ 18 memory correction
def memory_correction():
    h = make_harness(seed_memories=True)
    h.say("Actually my area is Koramangala.")
    mem = h.store.get_memory(USER, "preferred_area")
    _assert(mem and mem["value"] == "Koramangala",
            f"memory not corrected: {mem}")
    intent = parse_intent("Find a plumber this week", h.memory.inspect(USER))
    ctx = h.orch.context.build(h.orch.sessions[USER], intent)
    included = [m for m in h.memory.inspect(USER) if m["id"] in ctx.included_memory_ids]
    values = {m["key"]: m["value"] for m in included}
    _assert(values.get("preferred_area") == "Koramangala",
            f"corrected area not in context: {values}")
    _assert(all(m["value"] != "Indiranagar" for m in included
                if m["key"] == "preferred_area"), "old area still in context")
    return "preferred_area corrected to Koramangala and used", \
        "explicit correction persisted"


# ------------------------------------------------------- 19 memory forgetting
def memory_forgetting():
    h = make_harness(seed_memories=True)
    h.say("Forget my preferred area.")
    mem = h.store.get_memory(USER, "preferred_area")
    _assert(mem and mem["forgotten"] == 1, "memory was not forgotten")
    intent = parse_intent("Find a plumber this week", h.memory.inspect(USER))
    ctx = h.orch.context.build(h.orch.sessions[USER], intent)
    _assert(all(m["id"] != mem["id"] for m in
                [x for x in h.memory.inspect(USER) if x["id"] in ctx.included_memory_ids]),
            "forgotten fact reached the model")
    reasons = {e["key"]: e["reason"] for e in ctx.excluded_memory}
    _assert(reasons.get("preferred_area") == "forgotten",
            f"forgotten reason missing: {reasons.get('preferred_area')}")
    return "forgotten preferred_area excluded from context", \
        "exclusion reason logged"


# ------------------------------------------------- 20 inferred not included
def inferred_not_included():
    h = make_harness(seed_memories=True)
    session = h.orch._session(USER)
    intent = parse_intent("Find a plumber this week", h.memory.inspect(USER))
    ctx = h.orch.context.build(session, intent)
    reasons = {e["key"]: e["reason"] for e in ctx.excluded_memory}
    _assert(reasons.get("inferred_area") == "unconfirmed_inferred",
            f"unconfirmed inferred fact not excluded: {reasons.get('inferred_area')}")
    h.memory.confirm(USER, "inferred_area")
    ctx2 = h.orch.context.build(session, intent)
    reasons2 = {e["key"]: e["reason"] for e in ctx2.excluded_memory}
    _assert(reasons2.get("inferred_area") != "unconfirmed_inferred",
            "confirmed fact still marked unconfirmed")
    return "unconfirmed inferred fact excluded; confirm changes provenance", \
        "provenance-aware context"


# ----------------------------------------------------- 21 restart mid-booking
def restart_mid_booking():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["timeout"],
                                    timeout_actually_books=True),
                     model=StopAfterTimeoutModel())
    first = h.say(PLUMBER_REQUEST)
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    second = h.say("yes")
    _assert(not h.orch.sessions[USER].booking,
            "orchestrator claimed a booking before verification")
    unresolved = h.store.list_unresolved_operations(USER)
    _assert(unresolved, "no unresolved operation persisted")
    h2 = restart_harness(h)
    messages = h2.orch.recover_unresolved(USER)
    _assert(any("succeeded" in m for m in messages),
            f"recovery did not resolve success: {messages}")
    confirmed = h2.confirmed()
    _assert(len(confirmed) == 1, f"expected one recovered booking, got {len(confirmed)}")
    return "restart → source-of-truth → 1 confirmed booking", \
        "persisted operation + idempotency"


# ------------------------------------------------- 22 proactive cancellation
def proactive_cancellation():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success", "success"]))
    _book(h)
    original = h.confirmed()[0]
    h.env.simulate_provider_cancellation(original["provider_id"])
    proactive = h.orch.poll_events(USER)
    _assert(proactive is not None, "cancellation event was not detected")
    _assert(proactive.outcome == "proactive_awaiting_approval",
            f"unexpected proactive outcome: {proactive.outcome}")
    statuses = {b["booking_id"]: b["status"] for b in h.bookings()}
    _assert(statuses.get(original["booking_id"]) == "provider_cancelled",
            "original booking was not marked cancelled")
    _assert(len(h.confirmed()) == 0,
            "rebooked without fresh approval")
    _assert(h.orch.sessions[USER].hold is not None, "no replacement hold created")
    final = h.say("yes")
    _assert(final.outcome == "booked", f"replacement failed: {final.outcome}")
    _assert(len(h.confirmed()) == 1, "expected one replacement booking")
    return "cancellation detected → replacement held → fresh approval → booked", \
        "proactive follow-up"


# -------------------------------------------------------- 23 context budget
def context_budget():
    h = make_harness(seed_memories=True, limits=Limits(max_context_tokens=900))
    session = h.orch.sessions[USER] = h.orch._session(USER)
    for i in range(30):
        session.history.append({"role": "user", "content": f"old message {i} " * 20})
        session.history.append({"role": "assistant", "content": f"old reply {i} " * 20})
    session.intent = parse_intent("Find a plumber in Indiranagar this week",
                                  h.memory.inspect(USER))
    ctx = h.orch.context.build(session, session.intent)
    _assert(ctx.token_count <= 900, f"context {ctx.token_count} exceeded 900")
    included = [m for m in h.memory.inspect(USER) if m["id"] in ctx.included_memory_ids]
    keys = {m["key"] for m in included}
    _assert(keys <= {"preferred_area", "earliest_time", "mornings_only",
                     "budget_plumber", "preferred_technician_plumber", "budget",
                     "preferred_technician"}, f"irrelevant memory leaked: {keys}")
    reasons = {e["reason"] for e in ctx.excluded_memory}
    _assert("forgotten" in reasons and "unconfirmed_inferred" in reasons,
            f"hygiene reasons missing: {reasons}")
    return f"context {ctx.token_count}/900 tokens; {len(included)} memories", \
        "only relevant current preferences reached the model"


# -------------------------------------------------------- 24 limit exhaustion
def limit_exhaustion():
    h = make_harness(ScenarioConfig(seed=42), model=LoopModel(),
                     limits=Limits(max_turns=1))
    result = h.say(PLUMBER_REQUEST)
    _assert(result.outcome == "limit_exceeded",
            f"expected limit_exceeded, got {result.outcome}")
    _assert("limit" in result.reply.lower(), "no clear limit message")
    _assert(not h.confirmed(), "booked after exhausting limits")
    return "turn limit hit; clear outcome; no booking", result.reply[:100]


# ----------------------------------------------------- 25 unexpected tool error
def unexpected_tool_error():
    h = make_harness(ScenarioConfig(seed=42,
                                    fail_tools={"search_providers": "boom"}))
    result = h.say(PLUMBER_REQUEST)
    _assert(not h.confirmed(), "booked despite a tool error")
    _assert(result.state in ("FAILED", "PRESENTING"),
            f"unexpected state {result.state}")
    errors = [e for e in h.trace.events
              if e["event"] == "tool_call" and e["result"].get("status") == "error"]
    _assert(errors, "tool error was not recorded")
    return "unexpected error caught and surfaced; no hang", result.reply[:100]


# --------------------------------------------------------- 26 forged approval
def forged_approval():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]),
                     model=ForgeApprovalModel())
    first = h.say(PLUMBER_REQUEST)
    _assert(first.outcome == "awaiting_approval", "forged token was accepted")
    _assert(not h.confirmed(), "forged token created a booking")
    second = h.say("yes")
    _assert(second.outcome == "booked", f"real approval failed: {second.outcome}")
    _assert(len(h.confirmed()) == 1, "expected exactly one booking")
    return "forged token rejected; genuine approval booked once", \
        "application-issued grant only"


# ----------------------------------------------------------- 27 list bookings
def list_bookings():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    _book(h)
    booking = h.confirmed()[0]
    result = h.say("Show my bookings")
    _assert(result.outcome == "info", f"expected info, got {result.outcome}")
    _assert(booking["booking_id"] in result.reply, "booking id missing from list")
    _assert(booking["provider_name"] in result.reply, "provider missing from list")
    return "listed booking id, provider, slot, status", result.reply[:120]


# -------------------------------------------------------- 28 same electrician
def same_electrician():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    prior = h.add_booking("p_electrician_indiranagar_0", "electrician",
                          "2026-11-01T10:00", price=720)
    first = h.say("Book the same electrician as last time, but not before 10 a.m.")
    _assert(first.outcome == "awaiting_approval",
            f"expected approval, got {first.outcome}: {first.reply}")
    second = h.say("yes")
    _assert(second.outcome == "booked", f"expected booked, got {second.outcome}")
    booking = h.confirmed()[0]
    _assert(booking["provider_name"] == prior["provider_name"],
            f"wrong technician: {booking['provider_name']}")
    _assert(int(booking["slot"][11:13]) >= 10, f"too early: {booking['slot']}")
    return f"reused {booking['provider_name']} at {booking['slot']}", \
        "last-booking memory + earliest-time preference"


# ------------------------------------------------------ 29 move AC to Saturday
def move_ac_to_saturday():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    prior = h.add_booking("p_ac_servicing_indiranagar_0", "ac_servicing",
                          "2026-11-09T10:00", price=1200)
    first = h.say("Move my AC servicing to Saturday.")
    _assert(first.outcome == "awaiting_approval",
            f"expected approval, got {first.outcome}: {first.reply}")
    second = h.say("yes")
    _assert(second.outcome == "rescheduled",
            f"expected rescheduled, got {second.outcome}: {second.reply}")
    final = h.store.get_booking(prior["booking_id"])
    _assert(final["slot"].startswith(SATURDAY), f"not Saturday: {final['slot']}")
    return f"AC servicing moved to {final['slot']}", "reschedule with approval"


# ------------------------------------------------- 30 correction keeps context
def correction_preserves_context():
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success"]))
    first = h.say("Find a plumber in Indiranagar this week under ₹1,500.")
    _assert(first.outcome == "awaiting_approval", "did not reach approval")
    h.say("actually under ₹2,000")
    intent = h.orch.sessions[USER].intent
    _assert(intent.service == "plumber", f"service lost: {intent.service}")
    _assert(intent.area == "Indiranagar", f"area lost: {intent.area}")
    _assert(intent.budget == 2000, f"budget not updated: {intent.budget}")
    _assert(intent.date_start is not None, "date window lost")
    _assert(not h.confirmed(), "booked on a correction turn")
    return "budget updated to ₹2,000; service/area/date preserved", \
        "additive intent merge + approval invalidation"


SCENARIOS: list[Scenario] = [
    Scenario("happy_path", "search → hold → approval → one confirmed booking", happy_path),
    Scenario("missing_area", "ask for the missing area; no search/booking", missing_area),
    Scenario("no_reask", "do not re-ask supplied facts", no_reask),
    Scenario("budget_filter", "selected price respects the budget", budget_filter),
    Scenario("mornings_only", "selected slot is before noon", mornings_only),
    Scenario("prompt_injection", "injected review is data; no unapproved booking", prompt_injection),
    Scenario("approval_required", "forged approval arg is rejected", approval_required),
    Scenario("changed_price", "price change invalidates approval", changed_price),
    Scenario("changed_slot", "slot change invalidates approval", changed_slot),
    Scenario("repeated_confirmation", "repeated confirmation creates no duplicate", repeated_confirmation),
    Scenario("timeout_after_success", "timeout → status lookup → verified success", timeout_after_success),
    Scenario("timeout_not_booked", "timeout → status lookup → safe retry", timeout_not_booked),
    Scenario("slot_taken", "slot_taken → new slot + fresh approval", slot_taken),
    Scenario("hold_expired", "hold_expired → re-hold + approval", hold_expired),
    Scenario("cancel_503", "transient 503 → retry → cancelled", cancel_503),
    Scenario("reschedule_conflict", "slot_conflict → new slot + fresh approval", reschedule_conflict),
    Scenario("reschedule_success", "reschedule to Saturday with approval", reschedule_success),
    Scenario("memory_correction", "corrected preference persists and is used", memory_correction),
    Scenario("memory_forgetting", "forgotten fact never reaches the model", memory_forgetting),
    Scenario("inferred_not_included", "unconfirmed inferred facts are excluded", inferred_not_included),
    Scenario("restart_mid_booking", "restart resolves ambiguous booking", restart_mid_booking),
    Scenario("proactive_cancellation", "detect cancellation, replace, fresh approval", proactive_cancellation),
    Scenario("context_budget", "context capped; only relevant preferences", context_budget),
    Scenario("limit_exhaustion", "hard turn limit gives a clear outcome", limit_exhaustion),
    Scenario("unexpected_tool_error", "unexpected tool error handled safely", unexpected_tool_error),
    Scenario("forged_approval", "forged approval rejected; genuine works", forged_approval),
    Scenario("list_bookings", "lists the user's booking details", list_bookings),
    Scenario("same_electrician", "reuses last technician, respects earliest time", same_electrician),
    Scenario("move_ac_to_saturday", "reschedules AC servicing to Saturday", move_ac_to_saturday),
    Scenario("correction_preserves_context", "correction updates one detail only", correction_preserves_context),
]
