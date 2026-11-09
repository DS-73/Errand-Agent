"""Core data types shared by the errand agent.

The types here are deliberately plain dataclasses / dicts so they can be
serialised to JSON for traces and eval tables without extra dependencies.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


class ToolError(Exception):
    """A tool failure that is safe to show to the orchestration layer.

    ``code`` is either an HTTP-like integer or a short string such as
    ``"slot_conflict"``.  ``retryable`` tells the executor whether a bounded
    automatic retry is appropriate (e.g. a transient 503).
    """

    def __init__(self, code: int | str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"status": "error", "code": self.code, "message": self.message,
                "retryable": self.retryable}


@dataclass
class Intent:
    """The user's current errand intent.

    ``merge`` is intentionally additive: a correction to one detail must not
    wipe out unrelated details that were supplied earlier.
    """

    action: str = "book"  # book | cancel | reschedule | list
    service: str | None = None
    area: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    budget: int | None = None
    mornings_only: bool = False
    earliest_time: str | None = None
    preferred_technician: str | None = None
    target_slot: str | None = None
    same_as_last: bool = False
    raw: str = ""

    def missing(self) -> list[str]:
        if self.action != "book":
            return []
        missing = []
        if not self.service:
            missing.append("service")
        if not self.area:
            missing.append("area")
        if not self.date_start:
            missing.append("date_range")
        return missing

    def merge(self, other: "Intent") -> "Intent":
        """Return a copy of self updated with the non-None fields of other."""
        merged = Intent(**{k: v for k, v in self.__dict__.items() if k != "raw"})
        for key, value in other.__dict__.items():
            if key == "raw":
                continue
            if key == "action":
                # A new explicit action wins; default "book" does not overwrite
                # an existing cancel/reschedule intent.
                if value and value != "book":
                    merged.action = value
                continue
            if key in ("mornings_only",):
                if value:
                    merged.mornings_only = True
                continue
            if key == "same_as_last":
                if value:
                    merged.same_as_last = True
                continue
            if value is not None and value != "":
                setattr(merged, key, value)
        merged.raw = other.raw or self.raw
        # A correction to an afternoon time is incompatible with a stale
        # "mornings only" preference; drop the old constraint.
        if merged.earliest_time:
            try:
                if int(merged.earliest_time[:2]) >= 12:
                    merged.mornings_only = False
            except (TypeError, ValueError):
                pass
        return merged

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Intent":
        if not data:
            return cls()
        allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)


@dataclass(frozen=True)
class MaterialAction:
    """The exact consequential action a user must approve.

    The fingerprint is what approval is bound to.  Any change to provider,
    slot, price, booking id, or hold id produces a different fingerprint and
    therefore invalidates an earlier approval.
    """

    action: str
    provider_id: str | None = None
    slot: str | None = None
    price: int | None = None
    booking_id: str | None = None
    hold_id: str | None = None

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "action": self.action,
                "provider_id": self.provider_id,
                "slot": self.slot,
                "price": self.price,
                "booking_id": self.booking_id,
                "hold_id": self.hold_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["fingerprint"] = self.fingerprint()
        return data


@dataclass
class PendingApproval:
    user_id: str
    action: MaterialAction
    tool: str
    arguments: dict[str, Any]
    prompt: str
    created_at: float

    @property
    def fingerprint(self) -> str:
        return self.action.fingerprint()

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "action": self.action.to_dict(),
            "tool": self.tool,
            "arguments": self.arguments,
            "prompt": self.prompt,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass
class ApprovalGrant:
    user_id: str
    fingerprint: str
    action: MaterialAction
    tool: str
    arguments: dict[str, Any]
    created_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "fingerprint": self.fingerprint,
            "action": self.action.to_dict(),
            "tool": self.tool,
            "arguments": self.arguments,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class ModelResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


@dataclass
class SessionState:
    user_id: str
    state: str = "NEW"
    intent: Intent | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_provider_id: str | None = None
    hold: dict[str, Any] | None = None
    pending_approval: PendingApproval | None = None
    booking: dict[str, Any] | None = None
    bookings: list[dict[str, Any]] = field(default_factory=list)
    reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_tool: dict[str, Any] | None = None
    last_error: str | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    tool_calls: int = 0
    started_at: float = 0.0
    run_count: int = 0
    last_outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "state": self.state,
            "intent": self.intent.to_dict() if self.intent else None,
            "candidates": self.candidates,
            "selected_provider_id": self.selected_provider_id,
            "hold": self.hold,
            "pending_approval": self.pending_approval.to_dict() if self.pending_approval else None,
            "booking": self.booking,
            "bookings": self.bookings,
            "reviews": self.reviews,
            "last_tool": self.last_tool,
            "last_error": self.last_error,
            "flags": self.flags,
            "history": self.history,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "started_at": self.started_at,
            "run_count": self.run_count,
            "last_outcome": self.last_outcome,
        }


@dataclass
class Limits:
    max_turns: int = 8
    max_tool_calls: int = 14
    max_wall_clock_s: float = 90.0
    max_retries: int = 2
    max_context_tokens: int = 1200
    max_history_messages: int = 12
    approval_ttl_s: float = 300.0
    hold_ttl_s: float = 600.0
