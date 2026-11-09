"""FastAPI-facing adapter around the existing errand orchestrator.

The core agent stays framework-agnostic.  This module owns the small amount of
web-specific state: a lock so HTTP requests cannot interleave a turn, a mapping
from user to run ids for trace filtering, and JSON-safe payloads.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from errand_agent.approval import ApprovalGate
from errand_agent.context import ContextBuilder
from errand_agent.llm import Model, RuleBasedModel
from errand_agent.memory import MemoryStore
from errand_agent.mock_env import MockErrandEnvironment, ScenarioConfig
from errand_agent.models import Limits
from errand_agent.orchestrator import ErrandOrchestrator, TurnResult
from errand_agent.store import Store
from errand_agent.trace import TraceWriter, sanitize


@dataclass
class WebConfig:
    user: str = "u1"
    db: str = "data/web.db"
    trace: str | None = "traces/web.jsonl"
    seed: int = 42
    seed_memories: bool = True
    limits: Limits = field(default_factory=Limits)
    scenario: ScenarioConfig | None = None
    model: Model | None = None


class WebAgent:
    def __init__(self, config: WebConfig):
        self.config = config
        if config.db != ":memory:":
            Path(config.db).parent.mkdir(parents=True, exist_ok=True)

        self.store = Store(config.db)
        self.env = MockErrandEnvironment(
            self.store, config.scenario or ScenarioConfig(seed=config.seed))
        self.memory = MemoryStore(self.store)
        if config.seed_memories and not self.memory.inspect(config.user):
            self.memory.seed_demo_memories(config.user)

        self.approval = ApprovalGate(ttl_s=config.limits.approval_ttl_s)
        self.trace = TraceWriter(config.trace)
        self.orchestrator = ErrandOrchestrator(
            self.store,
            self.env,
            self.memory,
            self.approval,
            self.trace,
            config.model or RuleBasedModel(),
            config.limits,
            ContextBuilder(
                self.memory,
                max_tokens=config.limits.max_context_tokens,
                max_history=config.limits.max_history_messages,
            ),
        )

        # A turn mutates shared session state, so serialize HTTP turns.  SQLite
        # already has its own lock; this guards the orchestrator and trace list.
        self.lock = threading.RLock()
        self._runs: dict[str, list[str]] = {}
        self.recovery_messages = self.orchestrator.recover_unresolved(config.user)

    # ------------------------------------------------------------------ payloads
    def _record_run(self, user_id: str, run_id: str) -> None:
        runs = self._runs.setdefault(user_id, [])
        if run_id not in runs:
            runs.append(run_id)

    @staticmethod
    def _result_payload(result: TurnResult) -> dict[str, Any]:
        return {
            "reply": result.reply,
            "state": result.state,
            "outcome": result.outcome,
            "tool_calls": result.tool_calls,
            "booking": result.booking,
            "pending_approval": result.pending_approval,
            "run_id": result.run_id,
        }

    def session_info(self) -> dict[str, Any]:
        return {
            "user_id": self.config.user,
            "model": getattr(self.orchestrator.model, "name", "model"),
            "limits": asdict(self.config.limits),
            "recovery": self.recovery_messages,
        }

    # --------------------------------------------------------------------- turns
    def handle_message(self, user_id: str, text: str) -> dict[str, Any]:
        with self.lock:
            result = self.orchestrator.handle_message(user_id, text)
            self._record_run(user_id, result.run_id)
            return self._result_payload(result)

    def approve(self, user_id: str, decision: str) -> dict[str, Any]:
        with self.lock:
            if self.approval.get_pending(user_id) is None:
                return {
                    "reply": "There is no pending action to confirm. "
                             "Nothing was booked or changed.",
                    "state": "IDLE",
                    "outcome": "no_pending_approval",
                    "tool_calls": 0,
                    "booking": None,
                    "pending_approval": None,
                    "run_id": "",
                }
            text = "yes" if decision == "yes" else "no"
            result = self.orchestrator.handle_message(user_id, text)
            self._record_run(user_id, result.run_id)
            return self._result_payload(result)

    def poll_events(self, user_id: str) -> dict[str, Any]:
        with self.lock:
            result = self.orchestrator.poll_events(user_id)
            if result is None:
                return {"event": None}
            self._record_run(user_id, result.run_id)
            return {"event": self._result_payload(result)}

    # ------------------------------------------------------------------- queries
    def trace_events(self, user_id: str, limit: int = 300) -> list[dict[str, Any]]:
        with self.lock:
            run_ids = set(self._runs.get(user_id, []))
            events = [event for event in self.trace.events
                      if event.get("run_id") in run_ids]
            return [sanitize(event) for event in events[-limit:]]

    def memories(self, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return self.orchestrator.inspect_memories(user_id)

    def bookings(self, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return self.store.list_bookings(user_id)

    def state(self, user_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.orchestrator.sessions.get(user_id)
            if session is None:
                return {
                    "state": "NEW",
                    "intent": None,
                    "pending_approval": None,
                    "booking": None,
                    "bookings": self.store.list_bookings(user_id),
                    "last_outcome": None,
                    "flags": {},
                }
            return {
                "state": session.state,
                "intent": session.intent.to_dict() if session.intent else None,
                "pending_approval": session.pending_approval.to_dict()
                if session.pending_approval else None,
                "booking": session.booking,
                "bookings": self.store.list_bookings(user_id),
                "last_outcome": session.last_outcome,
                "flags": session.flags,
            }

    # ------------------------------------------------------------------- actions
    def forget(self, user_id: str, key: str) -> dict[str, Any]:
        with self.lock:
            return {"forgotten": self.orchestrator.forget_memory(user_id, key)}

    def simulate_cancel(self, user_id: str,
                        provider_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            confirmed = [b for b in self.store.list_bookings(user_id)
                         if b["status"] == "confirmed"]
            if provider_id is None:
                if not confirmed:
                    return {"affected": [], "event": None,
                            "message": "No confirmed booking to cancel."}
                provider_id = confirmed[0]["provider_id"]

            affected = self.env.simulate_provider_cancellation(
                provider_id, user_id=user_id)
            result = self.orchestrator.poll_events(user_id)
            if result is not None:
                self._record_run(user_id, result.run_id)
            return {
                "provider_id": provider_id,
                "affected": affected,
                "event": self._result_payload(result) if result else None,
            }

    def close(self) -> None:
        with self.lock:
            self.store.close()
