"""Explicit workflow orchestration around a bounded tool-calling model loop."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .approval import ApprovalGate
from .context import ContextBuilder
from .executor import ToolExecutor
from .llm import TOOL_SPECS, AgentView, Model, choose_candidate
from .memory import MemoryStore
from .mock_env import MockErrandEnvironment
from .models import Intent, Limits, MaterialAction, SessionState, ToolCall
from .nlu import describe_intent, has_change, is_affirmative, missing_prompt, parse_intent
from .store import Store
from .trace import TraceWriter, sanitize


@dataclass
class TurnResult:
    reply: str
    state: str
    run_id: str
    outcome: str
    tool_calls: int = 0
    booking: dict[str, Any] | None = None
    pending_approval: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.reply


class ErrandOrchestrator:
    def __init__(self, store: Store, env: MockErrandEnvironment, memory: MemoryStore,
                 approval: ApprovalGate, trace: TraceWriter, model: Model,
                 limits: Limits | None = None,
                 context_builder: ContextBuilder | None = None,
                 now_fn=None):
        self.store = store
        self.env = env
        self.memory = memory
        self.approval = approval
        self.trace = trace
        self.model = model
        self.limits = limits or Limits()
        self.context = context_builder or ContextBuilder(
            memory, max_tokens=self.limits.max_context_tokens,
            max_history=self.limits.max_history_messages)
        self.now = now_fn or time.time
        self.executor = ToolExecutor(env, store, approval, self.limits, trace,
                                     now_fn=self.now)
        self.sessions: dict[str, SessionState] = {}

    # ------------------------------------------------------------------ session
    def _session(self, user_id: str, now: float | None = None) -> SessionState:
        if user_id not in self.sessions:
            self.sessions[user_id] = SessionState(user_id=user_id, started_at=now or self.now())
        return self.sessions[user_id]

    # ------------------------------------------------------------- public turns
    def handle_message(self, user_id: str, text: str, now: float | None = None) -> TurnResult:
        now = now if now is not None else self.now()
        session = self._session(user_id, now)
        run_id = self.trace.start_run(user_id, "user_turn")
        session.run_count += 1
        session.turns = 0
        session.tool_calls = 0
        session.started_at = now
        session.last_tool = None
        session.last_error = None
        session.last_outcome = None
        for key in ("booked_this_run", "cancelled_this_run", "rescheduled_this_run"):
            session.flags.pop(key, None)
        session.history.append({"role": "user", "content": text})
        self.trace.log(run_id, "user_message", text=text)

        # Persistent memory: explicit preferences, corrections, forgetting.
        changes = self.memory.handle_user_statement(user_id, text, now=now)
        if changes:
            self.trace.log(run_id, "memory_changes", changes=changes)

        # Approval gate observes the raw user message, not model output.
        was_pending = self.approval.get_pending(user_id) is not None
        grant = self.approval.observe(user_id, text, now=now)
        if grant:
            session.flags["approval_granted"] = True
            self.trace.log(run_id, "approval_granted", fingerprint=grant.fingerprint,
                           action=grant.action.to_dict())
        elif was_pending and self.approval.last_event == "denied":
            session.pending_approval = None
            session.state = "IDLE"
            return self._finish(session, run_id,
                                "Okay — I won't proceed. Nothing was booked or changed.",
                                "cancelled_by_user")
        elif was_pending and self.approval.last_event == "invalidated_by_change":
            session.pending_approval = None
            self.trace.log(run_id, "approval_invalidated", reason="material_change")

        # Intent parsing + additive merge (a correction must not wipe context).
        memories = self.memory.inspect(user_id)
        new_intent = parse_intent(text, memories, today=date.fromtimestamp(now))
        if session.intent is None:
            session.intent = new_intent
        else:
            session.intent = session.intent.merge(new_intent)
            if new_intent.action != "book" or re.search(
                    r"\b(book|find|need|want|get|arrange)\b", text, re.IGNORECASE):
                session.intent.action = new_intent.action
        self.trace.log(run_id, "intent", intent=session.intent.to_dict())

        if session.intent.same_as_last:
            self._apply_last_booking(session, session.intent)

        # Ask only for genuinely missing details.
        if session.pending_approval is None and session.intent.action == "book" \
                and session.intent.missing():
            session.state = "CLARIFYING"
            return self._finish(session, run_id, missing_prompt(session.intent), "clarifying")

        return self._run_loop(session, run_id, text)

    # ------------------------------------------------------------------- loop
    def _run_loop(self, session: SessionState, run_id: str, user_text: str) -> TurnResult:
        while True:
            if session.turns >= self.limits.max_turns:
                return self._finish(
                    session, run_id,
                    "I've reached the limit for this request without completing it. "
                    "Nothing was booked or changed. Please try again with a narrower request.",
                    "limit_exceeded")
            if session.tool_calls >= self.limits.max_tool_calls:
                return self._finish(
                    session, run_id,
                    "I've reached the tool-call limit for this request. "
                    "Nothing further was changed.",
                    "limit_exceeded")
            if self.now() - session.started_at > self.limits.max_wall_clock_s:
                return self._finish(
                    session, run_id,
                    "I've run out of time for this request. Nothing further was changed.",
                    "timeout")

            session.turns += 1
            ctx = self.context.build(session, session.intent)
            self.trace.context(run_id, session.turns, ctx.messages, ctx.token_count,
                               ctx.included_memory_ids, ctx.excluded_memory)
            view = self._view(session, user_text)
            start = time.perf_counter()
            try:
                response = self.model.respond(ctx.messages, TOOL_SPECS, view)
            except Exception as exc:  # model/network failure must not hang
                latency = (time.perf_counter() - start) * 1000
                self.trace.log(run_id, "model_error", error=f"{type(exc).__name__}: {exc}",
                               latency_ms=round(latency, 3))
                return self._finish(session, run_id,
                                    "I couldn't reach the planning model. Nothing was changed. "
                                    "Please try again.", "model_error")
            latency = (time.perf_counter() - start) * 1000
            self.trace.model_call(run_id, session.turns, getattr(self.model, "name", "model"),
                                  {"content": response.content,
                                   "tool_calls": [c.to_dict() for c in response.tool_calls]},
                                  response.usage, latency)

            if not response.tool_calls:
                return self._finish(session, run_id,
                                    response.content or "I'm not sure how to proceed.",
                                    self._outcome(session))

            for call in response.tool_calls:
                old_state = session.state
                result = self.executor.execute(call, session, run_id, session.turns)
                session.last_tool = {"name": call.name, "result": result}
                session.history.append({
                    "role": "tool",
                    "name": call.name,
                    "content": json.dumps(sanitize(result), ensure_ascii=False),
                })
                self._apply_tool_result(session, call, result)
                if session.state != old_state:
                    self.trace.state_transition(run_id, old_state, session.state,
                                                tool=call.name)
                self.trace.log(run_id, "state_after_tool", state=session.state,
                               tool=call.name, status=result.get("status"))

                if result.get("status") == "approval_required":
                    return self._finish(session, run_id,
                                        result.get("prompt", "Please confirm the action."),
                                        "awaiting_approval")
                if result.get("status") == "limit_exceeded":
                    return self._finish(session, run_id, result.get("message", "Limit reached."),
                                        "limit_exceeded")

    # ------------------------------------------------------------------ helpers
    def _view(self, session: SessionState, user_text: str) -> AgentView:
        return AgentView(
            user_id=session.user_id,
            user_message=user_text,
            state=session.state,
            intent=session.intent.to_dict() if session.intent else None,
            candidates=session.candidates,
            selected_provider_id=session.selected_provider_id,
            hold=session.hold,
            pending_approval=session.pending_approval.to_dict()
            if session.pending_approval else None,
            booking=session.booking,
            bookings=session.bookings,
            reviews=session.reviews,
            last_tool=session.last_tool,
            flags=session.flags,
        )

    def _apply_last_booking(self, session: SessionState, intent: Intent) -> None:
        bookings = [b for b in self.store.list_bookings(session.user_id)
                    if b["status"] == "confirmed"]
        if intent.service:
            matching = [b for b in bookings if b["service"] == intent.service]
            if matching:
                bookings = matching
        if not bookings:
            return
        last = bookings[0]
        intent.preferred_technician = last.get("provider_name")
        intent.area = intent.area or self.env.provider_area(last["provider_id"])
        if not intent.date_start:
            today = date.fromtimestamp(self.now())
            intent.date_start = today.isoformat()
            intent.date_end = (today + timedelta(days=6)).isoformat()

    def _apply_tool_result(self, session: SessionState, call: ToolCall,
                           result: dict[str, Any]) -> None:
        name = call.name
        status = result.get("status")
        if name == "search_providers":
            if status == "ok":
                session.candidates = result.get("providers") or []
                session.state = "PRESENTING"
                session.hold = None
                session.selected_provider_id = None
            else:
                session.last_error = result.get("message")
                session.state = "FAILED"
        elif name == "get_provider":
            if status == "ok":
                provider_id = result.get("id")
                session.reviews[provider_id] = result
                checked = session.flags.setdefault("reviews_checked", [])
                if provider_id not in checked:
                    checked.append(provider_id)
            else:
                session.last_error = result.get("message")
        elif name == "hold_slot":
            if status == "held":
                session.hold = result
                session.selected_provider_id = result.get("provider_id")
                session.state = "HOLDING"
            elif status in ("slot_unavailable", "not_found"):
                session.last_error = result.get("message")
                session.state = "SEARCHING"
                session.hold = None
        elif name == "book":
            if status == "confirmed":
                booking = result.get("booking") or {}
                session.booking = booking
                session.state = "BOOKED"
                session.hold = None
                session.pending_approval = None
                session.flags["booked_this_run"] = True
                session.flags.pop("conflict_slots", None)
                self.memory.record_booking_inferences(
                    session.user_id,
                    {**booking, "area": self.env.provider_area(booking.get("provider_id", ""))})
            elif status == "timeout":
                session.state = "BOOKING"
            elif status == "slot_taken":
                session.state = "SEARCHING"
                if session.hold:
                    session.flags.setdefault("conflict_slots", []).append(session.hold.get("slot"))
                session.hold = None
            elif status == "hold_expired":
                session.state = "SEARCHING"
                session.flags["last_hold"] = session.hold
                session.hold = None
            elif status == "approval_required":
                session.state = "AWAITING_APPROVAL"
            elif status == "error":
                session.last_error = result.get("message")
                session.state = "FAILED"
        elif name == "get_booking_status":
            if status == "confirmed":
                session.booking = result.get("booking") or {}
                session.state = "BOOKED"
                session.hold = None
                session.pending_approval = None
                session.flags["booked_this_run"] = True
            elif status == "not_booked":
                session.state = "BOOKING"
            elif status == "failed":
                session.state = "FAILED"
                session.last_error = result.get("reason")
        elif name == "list_bookings":
            if status == "ok":
                session.bookings = result.get("bookings") or []
                target = self._pick_target(session.bookings, session.intent)
                if target:
                    session.flags["target_booking"] = target
                session.state = "PRESENTING"
        elif name == "cancel":
            if status == "cancelled":
                session.booking = result.get("booking") or session.booking
                session.state = "DONE"
                session.flags["cancelled_this_run"] = True
                session.pending_approval = None
                session.bookings = self.store.list_bookings(session.user_id)
            elif status == "error":
                session.last_error = result.get("message")
                session.state = "FAILED"
        elif name == "reschedule":
            if status == "rescheduled":
                session.booking = result.get("booking") or {}
                session.state = "BOOKED"
                session.flags["rescheduled_this_run"] = True
                session.pending_approval = None
                session.bookings = self.store.list_bookings(session.user_id)
            elif status == "slot_conflict":
                session.state = "PRESENTING"
                session.flags.setdefault("conflict_slots", []).append(call.arguments.get("slot"))
                session.pending_approval = None
            elif status == "approval_required":
                session.state = "AWAITING_APPROVAL"

    def _pick_target(self, bookings: list[dict[str, Any]], intent: Intent | None) -> dict | None:
        confirmed = [b for b in bookings if b["status"] == "confirmed"]
        if intent and intent.service:
            matching = [b for b in confirmed if b["service"] == intent.service]
            if matching:
                confirmed = matching
        if not confirmed:
            return None
        target = dict(confirmed[0])
        target["area"] = self.env.provider_area(target.get("provider_id", "")) or \
            (intent.area if intent else None)
        return target

    def _outcome(self, session: SessionState) -> str:
        if session.flags.get("cancelled_this_run") or session.state == "DONE":
            return "cancelled"
        if session.flags.get("rescheduled_this_run"):
            return "rescheduled"
        if session.flags.get("booked_this_run") or session.state == "BOOKED":
            return "booked"
        if session.state == "AWAITING_APPROVAL":
            return "awaiting_approval"
        if session.state == "FAILED":
            return "failed"
        if session.state == "CLARIFYING":
            return "clarifying"
        return "info"

    def _finish(self, session: SessionState, run_id: str, reply: str,
                outcome: str) -> TurnResult:
        session.last_outcome = outcome
        if not session.history or session.history[-1].get("content") != reply:
            session.history.append({"role": "assistant", "content": reply})
        self.trace.outcome(run_id, outcome, state=session.state, reply=reply)
        return TurnResult(
            reply=reply,
            state=session.state,
            run_id=run_id,
            outcome=outcome,
            tool_calls=session.tool_calls,
            booking=session.booking,
            pending_approval=session.pending_approval.to_dict()
            if session.pending_approval else None,
        )

    # --------------------------------------------------------------- recovery
    def recover_unresolved(self, user_id: str) -> list[str]:
        """Resolve operations left in flight by a previous process.

        Called on startup (and by the restart test).  Always queries the source
        of truth; never assumes a timeout means failure or success.
        """
        messages = []
        for op in self.store.list_unresolved_operations(user_id):
            key = op["idempotency_key"]
            status = self.env.get_booking_status(idempotency_key=key)
            if status.get("status") == "confirmed":
                booking = status.get("booking") or {}
                self.store.upsert_operation(key, user_id, op["action"], op["fingerprint"],
                                            "confirmed", booking_id=booking.get("booking_id"))
                if op["action"] == "book":
                    session = self._session(user_id)
                    session.booking = booking
                    session.state = "BOOKED"
                messages.append(f"Recovered: {op['action']} {key} actually succeeded.")
            elif status.get("status") == "not_booked":
                self.store.upsert_operation(key, user_id, op["action"], op["fingerprint"],
                                            "failed", detail="not_booked_after_timeout")
                messages.append(f"Recovered: {op['action']} {key} did not go through.")
            else:
                messages.append(f"Recovery pending for {op['action']} {key}.")
        return messages

    # --------------------------------------------------------------- proactive
    def poll_events(self, user_id: str) -> TurnResult | None:
        """Detect a simulated provider cancellation without a user prompt."""
        events = self.store.list_events(user_id, unconsumed_only=True)
        if not events:
            return None
        event = events[0]
        self.store.mark_event_consumed(event["id"])
        session = self._session(user_id)
        run_id = self.trace.start_run(user_id, "proactive_event")
        self.trace.log(run_id, "event", kind=event["kind"], payload=event["payload"])

        if event["kind"] != "provider_cancelled":
            return self._finish(session, run_id, "I noticed an update on your booking.",
                                "proactive_info")

        payload = event["payload"]
        service = payload.get("service") or "service"
        area = self.env.provider_area(payload.get("provider_id", ""))
        today = date.fromtimestamp(self.now())
        intent = Intent(action="book", service=service, area=area,
                        date_start=today.isoformat(),
                        date_end=(today + timedelta(days=6)).isoformat(),
                        raw="proactive replacement")
        included, _ = self.memory.relevant(user_id, intent)
        for mem in included:
            if mem["key"] == "earliest_time":
                intent.earliest_time = mem["value"]
            elif mem["key"] == "mornings_only":
                intent.mornings_only = True
            elif mem["key"] == f"budget_{service}":
                try:
                    intent.budget = int(mem["value"])
                except (TypeError, ValueError):
                    pass
        result = self.env.search_providers(service, area,
                                           {"start": intent.date_start, "end": intent.date_end})
        providers = [p for p in (result.get("providers") or [])
                     if p["id"] != payload.get("provider_id")]
        candidate, slot, reason = choose_candidate(providers, intent)
        if candidate is None:
            msg = (f"Heads up — {payload.get('provider_name') or 'your provider'} cancelled "
                   f"your {service.replace('_', ' ')} booking on {payload.get('slot')}. "
                   "I couldn't find a replacement within your saved preferences. "
                   "Would you like to widen the time or budget?")
            session.history.append({"role": "assistant", "content": msg})
            return self._finish(session, run_id, msg, "proactive_no_replacement")

        hold = self.env.hold_slot(candidate["id"], slot, user_id=user_id)
        action = MaterialAction("book", provider_id=candidate["id"], slot=slot,
                                price=hold["price"], hold_id=hold["hold_id"])
        prompt = (f"Heads up — {payload.get('provider_name') or 'your provider'} cancelled "
                  f"your {service.replace('_', ' ')} booking on {payload.get('slot')}. "
                  f"I found {candidate['name']} ({candidate['rating']}★) on {slot} "
                  f"for ₹{hold['price']:,}. "
                  + ApprovalGate.format_prompt(action, candidate["name"]))
        pending = self.approval.request(user_id, action, "book",
                                        {"hold_id": hold["hold_id"],
                                         "idempotency_key": "auto"},
                                        prompt, now=self.now())
        session.intent = intent
        session.candidates = providers
        session.hold = hold
        session.selected_provider_id = candidate["id"]
        session.pending_approval = pending
        session.state = "AWAITING_APPROVAL"
        session.flags["proactive"] = True
        session.history.append({"role": "assistant", "content": prompt})
        return self._finish(session, run_id, prompt, "proactive_awaiting_approval")

    # ------------------------------------------------------------------ memory
    def inspect_memories(self, user_id: str) -> list[dict[str, Any]]:
        return self.memory.inspect(user_id)

    def forget_memory(self, user_id: str, key: str) -> bool:
        return self.store.forget_memory(user_id, key)
