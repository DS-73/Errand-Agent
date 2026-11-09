"""Structured JSONL tracing.

Every run gets a run_id.  Each event is one JSON line.  Tool results are
sanitised (truncated, control characters stripped, secrets redacted) before
being written so a prompt-injection payload cannot leak into downstream logs
as an executable instruction.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

_SECRET_RE = re.compile(r"(api[_-]?key|authorization|token|secret)\s*[:=]\s*\S+", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(value: Any, max_len: int = 800) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v, max_len) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, max_len) for v in value]
    if isinstance(value, str):
        cleaned = _CONTROL_RE.sub(" ", value)
        cleaned = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", cleaned)
        cleaned = cleaned.replace("\n", " ").strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len] + "…"
        return cleaned
    return value


class TraceWriter:
    def __init__(self, path: str | None = None, cost_per_1k_prompt: float = 0.0005,
                 cost_per_1k_completion: float = 0.0015):
        self.path = path
        self.cost_per_1k_prompt = cost_per_1k_prompt
        self.cost_per_1k_completion = cost_per_1k_completion
        self.events: list[dict[str, Any]] = []
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            # Start a fresh trace file for each process.
            open(path, "w", encoding="utf-8").close()

    def start_run(self, user_id: str, kind: str = "user_turn") -> str:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        self.log(run_id, "run_start", user_id=user_id, kind=kind)
        return run_id

    def log(self, run_id: str, event: str, **data: Any) -> dict[str, Any]:
        record = {
            "run_id": run_id,
            "ts": round(time.time(), 6),
            "event": event,
            **sanitize(data),
        }
        self.events.append(record)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def state_transition(self, run_id: str, old: str, new: str, **data: Any) -> None:
        self.log(run_id, "state_transition", **{"from": old, "to": new, **data})

    def context(self, run_id: str, turn: int, messages: list[dict[str, Any]],
                token_count: int, included_ids: list[int], excluded: list[dict[str, Any]]) -> None:
        self.log(run_id, "context", turn=turn, token_count=token_count,
                 messages=messages, included_memory_ids=included_ids,
                 excluded_memory=[{"id": e["id"], "key": e["key"], "reason": e["reason"]}
                                  for e in excluded])

    def model_call(self, run_id: str, turn: int, model: str, response: dict[str, Any],
                   usage: dict[str, int], latency_ms: float) -> None:
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        cost = (prompt_tokens / 1000.0) * self.cost_per_1k_prompt + \
               (completion_tokens / 1000.0) * self.cost_per_1k_completion
        self.log(run_id, "model_call", turn=turn, model=model, response=response,
                 usage=usage, latency_ms=round(latency_ms, 3),
                 estimated_cost_usd=round(cost, 8))

    def tool_call(self, run_id: str, turn: int, name: str, arguments: dict[str, Any],
                  result: dict[str, Any], latency_ms: float, attempt: int = 1) -> None:
        self.log(run_id, "tool_call", turn=turn, name=name, arguments=arguments,
                 result=result, latency_ms=round(latency_ms, 3), attempt=attempt)

    def outcome(self, run_id: str, outcome: str, **data: Any) -> None:
        self.log(run_id, "outcome", outcome=outcome, **data)
