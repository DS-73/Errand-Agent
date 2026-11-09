"""Generate the end-to-end trace for a timeout-after-success booking.

Run: python evals/generate_trace.py
Writes: traces/e2e_timeout_after_success.jsonl
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from errand_agent.mock_env import ScenarioConfig
from errand_agent.testing import make_harness

REQUEST = ("Find a plumber in Indiranagar this week, mornings only, "
           "under ₹1,500.")


def main() -> int:
    h = make_harness(
        ScenarioConfig(seed=42, book_outcomes=["timeout"],
                       timeout_actually_books=True),
        trace_path="traces/e2e_timeout_after_success.jsonl",
    )
    first = h.say(REQUEST)
    print("turn 1:", first.state, first.outcome)
    print(first.reply)
    second = h.say("yes")
    print("turn 2:", second.state, second.outcome)
    print(second.reply)
    print("bookings:", [(b["booking_id"], b["status"]) for b in h.bookings()])
    print("trace: traces/e2e_timeout_after_success.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
