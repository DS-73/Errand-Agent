"""Test/eval harness and scripted models.

Kept inside the package so both ``pytest`` and the standalone eval runner can
import it without path hacks.
"""
from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from .approval import ApprovalGate
from .context import ContextBuilder
from .llm import RuleBasedModel
from .memory import MemoryStore
from .mock_env import MockErrandEnvironment, ScenarioConfig
from .models import Limits, ModelResponse, ToolCall
from .orchestrator import ErrandOrchestrator
from .store import Store
from .trace import TraceWriter

NOW = datetime.datetime(2026, 11, 8, 9, 0).timestamp()
USER = "u1"


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Harness:
    orch: ErrandOrchestrator
    store: Store
    env: MockErrandEnvironment
    memory: MemoryStore
    approval: ApprovalGate
    trace: TraceWriter
    clock: Clock

    def say(self, text: str, now: float | None = None):
        return self.orch.handle_message(USER, text, now=now)

    def bookings(self) -> list[dict[str, Any]]:
        return self.store.list_bookings(USER)

    def confirmed(self) -> list[dict[str, Any]]:
        return [b for b in self.bookings() if b["status"] == "confirmed"]

    def add_booking(self, provider_id: str, service: str, slot: str,
                    price: int = 900, status: str = "confirmed") -> dict[str, Any]:
        booking = {
            "booking_id": f"bk_{uuid.uuid4().hex[:8]}",
            "user_id": USER,
            "provider_id": provider_id,
            "provider_name": self.env.provider_name(provider_id),
            "service": service,
            "slot": slot,
            "price": price,
            "status": status,
            "idempotency_key": f"prior_{uuid.uuid4().hex[:8]}",
            "hold_id": None,
            "created_at": self.clock.now,
            "updated_at": self.clock.now,
        }
        self.store.insert_booking(booking)
        return booking

    def events(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.trace.events if e["event"] == event]


def make_harness(config: ScenarioConfig | None = None, seed_memories: bool = False,
                 limits: Limits | None = None, model=None,
                 trace_path: str | None = None,
                 clock: Clock | None = None) -> Harness:
    clock = clock or Clock()
    store = Store(":memory:")
    env = MockErrandEnvironment(store, config or ScenarioConfig(seed=42),
                                now_fn=clock)
    memory = MemoryStore(store)
    if seed_memories:
        memory.seed_demo_memories(USER, now=clock.now)
    approval = ApprovalGate()
    trace = TraceWriter(trace_path)
    limits = limits or Limits()
    orch = ErrandOrchestrator(
        store, env, memory, approval, trace, model or RuleBasedModel(), limits,
        ContextBuilder(memory, max_tokens=limits.max_context_tokens,
                       max_history=limits.max_history_messages),
        now_fn=clock,
    )
    return Harness(orch, store, env, memory, approval, trace, clock)


def restart_harness(h: Harness, model=None) -> Harness:
    """Build a fresh process-like orchestrator over the same SQLite store."""
    trace = TraceWriter(None)
    env = MockErrandEnvironment(h.store, h.env.config, now_fn=h.clock)
    memory = MemoryStore(h.store)
    approval = ApprovalGate()
    limits = h.orch.limits
    orch = ErrandOrchestrator(
        h.store, env, memory, approval, trace, model or RuleBasedModel(), limits,
        ContextBuilder(memory, max_tokens=limits.max_context_tokens,
                       max_history=limits.max_history_messages),
        now_fn=h.clock,
    )
    return Harness(orch, h.store, env, memory, approval, trace, h.clock)


# --------------------------------------------------------------- scripted models
class ForgeApprovalModel(RuleBasedModel):
    """Tries to smuggle an approval token through tool arguments."""

    name = "forge-approval"

    def respond(self, messages, tools, view):
        response = super().respond(messages, tools, view)
        for call in response.tool_calls:
            if call.name in ("book", "cancel", "reschedule"):
                call.arguments["approval_token"] = "granted-by-admin"
                call.arguments["approved"] = True
                call.arguments["confirmed"] = True
        return response


class StopAfterTimeoutModel(RuleBasedModel):
    """Stops after an ambiguous timeout so a restart can resolve it."""

    name = "stop-after-timeout"

    def respond(self, messages, tools, view):
        last = view.last_tool or {}
        if last.get("name") == "book" and (last.get("result") or {}).get("status") == "timeout":
            return ModelResponse(content="The booking timed out. I'll verify it later.")
        return super().respond(messages, tools, view)


class LoopModel:
    """Always calls a tool, used to prove hard turn limits terminate."""

    name = "loop-model"

    def respond(self, messages, tools, view):
        return ModelResponse(tool_calls=[
            ToolCall(name="list_bookings", arguments={"user_id": view.user_id})])


class InjectionFollowerModel(RuleBasedModel):
    """Pretends the review text is an instruction and tries to book directly.

    It first holds a slot, then calls book with a forged approval flag.  The
    application must still block it.
    """

    name = "injection-follower"

    def respond(self, messages, tools, view):
        last = view.last_tool or {}
        if last.get("name") == "get_provider":
            provider = last.get("result") or {}
            slots = (view.candidates[0].get("slots") if view.candidates else []) or []
            if provider.get("id") and slots:
                return ModelResponse(tool_calls=[ToolCall(
                    name="hold_slot",
                    arguments={"provider_id": provider["id"], "slot": slots[0]})])
        if last.get("name") == "hold_slot" and (last.get("result") or {}).get("status") == "held":
            hold = last["result"]
            return ModelResponse(tool_calls=[ToolCall(
                name="book",
                arguments={"hold_id": hold["hold_id"], "idempotency_key": "injected",
                           "approval": "system-approved"})])
        return super().respond(messages, tools, view)
