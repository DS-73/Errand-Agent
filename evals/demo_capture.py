"""Capture a scripted 3–5 minute demo as text (chat + trace side by side).

Run: python evals/demo_capture.py
Writes: demo_transcript.md and traces/demo.jsonl
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from errand_agent.mock_env import ScenarioConfig
from errand_agent.testing import make_harness


def trace_excerpt(h, events=("state_transition", "tool_call", "outcome")):
    rows = []
    for e in h.trace.events:
        if e["event"] not in events:
            continue
        if e["event"] == "tool_call":
            rows.append(f"tool  {e['name']:<20} → {e['result'].get('status')} "
                        f"({e.get('latency_ms', 0)} ms)")
        elif e["event"] == "state_transition":
            rows.append(f"state {e['from']} → {e['to']}")
        else:
            rows.append(f"outcome {e['outcome']} ({e['state']})")
    return "\n".join(rows)


def main() -> int:
    h = make_harness(ScenarioConfig(seed=42, book_outcomes=["success", "success"]),
                     trace_path="traces/demo.jsonl")
    lines = ["# Demo transcript (text capture)", "",
             "A full booking plus a live proactive cancellation recovery. "
             "Run the same flow interactively with `python -m errand_agent`.", ""]

    lines += ["## Act 1 — full booking", "", "```text", "you> Find a plumber in "
              "Indiranagar this week, mornings only, under ₹1,500."]
    r1 = h.say("Find a plumber in Indiranagar this week, mornings only, under ₹1,500.")
    lines.append(f"agent> {r1.reply}")
    lines.append("")
    lines.append("you> yes")
    r2 = h.say("yes")
    lines.append(f"agent> {r2.reply}")
    lines.append("```")
    lines += ["", "Trace (excerpt):", "", "```text", trace_excerpt(h), "```"]

    booking = h.confirmed()[0]
    lines += ["", "## Act 2 — provider cancels; agent acts without a prompt", "",
              "```text",
              f"[simulated event] {booking['provider_name']} cancelled booking "
              f"{booking['booking_id']} on {booking['slot']}."]
    h.env.simulate_provider_cancellation(booking["provider_id"])
    proactive = h.orch.poll_events("u1")
    lines.append(f"agent> {proactive.reply}")
    lines.append("")
    lines.append("you> yes")
    replacement = h.say("yes")
    lines.append(f"agent> {replacement.reply}")
    lines.append("```")
    lines += ["", "Trace (excerpt):", "", "```text", trace_excerpt(h), "```"]
    lines += ["", "## Final bookings", "", "```text"]
    for b in h.bookings():
        lines.append(f"{b['booking_id']}  {b['provider_name']:<22} {b['slot']}  "
                     f"{b['status']}")
    lines.append("```")
    lines += ["", "The original booking is `provider_cancelled`; the replacement is "
              "`confirmed` only after a fresh approval.", ""]

    with open("demo_transcript.md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
