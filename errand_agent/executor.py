"""Tool execution boundary: budgets, approval enforcement, idempotency, retries."""
from __future__ import annotations

import hashlib
import time
from typing import Any

from .approval import ApprovalGate
from .mock_env import MockErrandEnvironment
from .models import (ApprovalGrant, Limits, MaterialAction, SessionState, ToolCall,
                     ToolError)
from .store import Store
from .trace import TraceWriter, sanitize

CONSEQUENTIAL = {"book", "cancel", "reschedule"}


def effective_idempotency_key(user_id: str, action: MaterialAction) -> str:
    """Application-derived key. Model-supplied keys are never trusted.

    Binding the key to the material fingerprint means repeated confirmations
    and network retries collapse onto the same server-side operation, while a
    changed provider/slot/price produces a new key.
    """
    payload = f"{user_id}|{action.action}|{action.fingerprint()}"
    return "idem_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class ToolExecutor:
    def __init__(self, env: MockErrandEnvironment, store: Store,
                 approval: ApprovalGate, limits: Limits, trace: TraceWriter,
                 now_fn=None):
        self.env = env
        self.store = store
        self.approval = approval
        self.limits = limits
        self.trace = trace
        self.now = now_fn or time.time

    # ------------------------------------------------------------------ public
    def execute(self, call: ToolCall, session: SessionState, run_id: str,
                turn: int) -> dict[str, Any]:
        if session.tool_calls >= self.limits.max_tool_calls:
            return {"status": "limit_exceeded",
                    "message": "The tool-call limit for this turn has been reached."}
        session.tool_calls += 1

        name = call.name
        args = dict(call.arguments or {})

        if name not in {"search_providers", "get_provider", "hold_slot", "book",
                        "get_booking_status", "cancel", "reschedule", "list_bookings"}:
            return {"status": "error", "code": "unknown_tool",
                    "message": f"Unknown tool {name!r}"}

        grant: ApprovalGrant | None = None
        if name in CONSEQUENTIAL:
            action, normalized = self._material_action(name, args)
            if action is None:
                # Let the mock environment produce its normal not-found error.
                pass
            else:
                grant = self.approval.grant_for(session.user_id, action)
                if grant is None or not self.approval.has_grant(session.user_id, action,
                                                               now=self.now()):
                    pending = self.approval.request(
                        session.user_id, action, name, normalized,
                        ApprovalGate.format_prompt(action, normalized.get("provider_name")),
                    )
                    session.pending_approval = pending
                    session.state = "AWAITING_APPROVAL"
                    result = {
                        "status": "approval_required",
                        "action": action.to_dict(),
                        "prompt": pending.prompt,
                        "pending": pending.to_dict(),
                    }
                    self.trace.tool_call(run_id, turn, name, args, result, 0.0)
                    return result
                # Consume on the attempt; restore only for an ambiguous timeout.
                self.approval.consume(session.user_id, action)
                self.approval.clear_pending(session.user_id)
                session.pending_approval = None
                args = normalized
                key = effective_idempotency_key(session.user_id, action)
                args["idempotency_key"] = key
                if name == "book":
                    self.store.upsert_operation(key, session.user_id, "book",
                                                action.fingerprint(), "pending",
                                                hold_id=action.hold_id)
                elif name == "cancel":
                    self.store.upsert_operation(key, session.user_id, "cancel",
                                                action.fingerprint(), "pending",
                                                booking_id=action.booking_id)
                elif name == "reschedule":
                    self.store.upsert_operation(key, session.user_id, "reschedule",
                                                action.fingerprint(), "pending",
                                                booking_id=action.booking_id)

        args = self._normalize(name, args, session)
        result = self._invoke(name, args, run_id, turn)

        if name == "book" and result.get("status") == "timeout" and grant is not None:
            # Same exact action, same idempotency key: safe to retry after the
            # source-of-truth lookup says nothing was booked.
            self.approval.restore(grant, now=self.now())

        if name in CONSEQUENTIAL and "idempotency_key" in args:
            key = args["idempotency_key"]
            status = result.get("status")
            if status == "confirmed":
                booking = result.get("booking") or {}
                self.store.upsert_operation(key, session.user_id, name,
                                            self._fingerprint_for(name, args),
                                            "confirmed", booking_id=booking.get("booking_id"),
                                            hold_id=args.get("hold_id"))
            elif status == "timeout":
                self.store.upsert_operation(key, session.user_id, name,
                                            self._fingerprint_for(name, args),
                                            "timeout", hold_id=args.get("hold_id"))
            elif status in ("slot_taken", "hold_expired", "slot_conflict", "error"):
                self.store.upsert_operation(key, session.user_id, name,
                                            self._fingerprint_for(name, args),
                                            "failed", detail=status)
            elif status in ("cancelled", "rescheduled"):
                self.store.upsert_operation(key, session.user_id, name,
                                            self._fingerprint_for(name, args),
                                            "confirmed", detail=status)

        return result

    # ------------------------------------------------------------------ helpers
    def _fingerprint_for(self, name: str, args: dict[str, Any]) -> str:
        action, _ = self._material_action(name, args)
        return action.fingerprint() if action else ""

    def _material_action(self, name: str, args: dict[str, Any]) -> tuple[MaterialAction | None, dict[str, Any]]:
        if name == "book":
            hold = self.store.get_hold(args.get("hold_id", ""))
            if not hold:
                return None, args
            action = MaterialAction("book", provider_id=hold["provider_id"],
                                    slot=hold["slot"], price=hold["price"],
                                    hold_id=hold["hold_id"])
            normalized = {"hold_id": hold["hold_id"], "idempotency_key": "auto",
                          "provider_name": self.env.provider_name(hold["provider_id"])}
            return action, normalized
        if name == "cancel":
            booking = self.store.get_booking(args.get("booking_id", ""))
            if not booking:
                return None, args
            action = MaterialAction("cancel", provider_id=booking["provider_id"],
                                    slot=booking["slot"], price=booking["price"],
                                    booking_id=booking["booking_id"])
            normalized = {"booking_id": booking["booking_id"],
                          "idempotency_key": "auto",
                          "provider_name": booking.get("provider_name")}
            return action, normalized
        if name == "reschedule":
            booking = self.store.get_booking(args.get("booking_id", ""))
            if not booking:
                return None, args
            action = MaterialAction("reschedule", provider_id=booking["provider_id"],
                                    slot=args.get("slot"), price=booking["price"],
                                    booking_id=booking["booking_id"])
            normalized = {"booking_id": booking["booking_id"],
                          "slot": args.get("slot"), "idempotency_key": "auto",
                          "provider_name": booking.get("provider_name")}
            return action, normalized
        return None, args

    def _normalize(self, name: str, args: dict[str, Any], session: SessionState) -> dict[str, Any]:
        args = dict(args)
        args.pop("provider_name", None)
        if name == "search_providers":
            dr = args.get("date_range")
            if isinstance(dr, str):
                args["date_range"] = {"start": dr, "end": dr}
        elif name == "hold_slot":
            args["user_id"] = session.user_id
        elif name == "list_bookings":
            args["user_id"] = session.user_id
        elif name == "get_booking_status":
            args.pop("provider_name", None)
        # Strip any model-supplied approval material: it is never authoritative.
        for key in list(args):
            if key in ("approval", "approval_token", "approved", "confirmed"):
                args.pop(key, None)
        return args

    def _invoke(self, name: str, args: dict[str, Any], run_id: str,
                turn: int) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            start = time.perf_counter()
            try:
                result = getattr(self.env, name)(**args)
                latency = (time.perf_counter() - start) * 1000
                self.trace.tool_call(run_id, turn, name, args, sanitize(result), latency,
                                     attempt=attempt)
                return result
            except ToolError as exc:
                latency = (time.perf_counter() - start) * 1000
                self.trace.tool_call(run_id, turn, name, args, exc.to_dict(), latency,
                                     attempt=attempt)
                if exc.retryable and attempt <= self.limits.max_retries:
                    continue
                return exc.to_dict()
            except Exception as exc:  # unexpected tool failure
                latency = (time.perf_counter() - start) * 1000
                payload = {"status": "error", "code": "unexpected",
                           "message": f"{type(exc).__name__}: {exc}"}
                self.trace.tool_call(run_id, turn, name, args, payload, latency,
                                     attempt=attempt)
                return payload
