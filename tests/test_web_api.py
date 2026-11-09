"""HTTP-level tests for the FastAPI adapter.

These exercise the same orchestrator through the web boundary: token auth,
a full approval-gated booking, trace filtering, memory inspection, and the
proactive cancellation flow.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errand_agent.mock_env import ScenarioConfig
from errand_agent.web.webapp.backend.api import create_app
from errand_agent.web.webapp.backend.service import WebConfig

REQUEST = "Find a plumber in Indiranagar this week, mornings only, under ₹1,500."


@pytest.fixture()
def client():
    app = create_app(WebConfig(
        user="u1",
        db=":memory:",
        trace=None,
        seed_memories=True,
        scenario=ScenarioConfig(seed=42, book_outcomes=["success", "success"]),
    ))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def token(client):
    response = client.get("/api/session")
    assert response.status_code == 200
    return response.json()["token"]


def auth(token: str) -> dict[str, str]:
    return {"X-Web-Token": token}


def test_health_is_public(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_session_returns_token_and_limits(client):
    body = client.get("/api/session").json()
    assert body["token"]
    assert body["user_id"] == "u1"
    assert body["limits"]["max_turns"] >= 1
    assert isinstance(body["recovery"], list)


def test_token_is_required(client):
    assert client.get("/api/trace").status_code == 403
    assert client.post("/api/message", json={"text": "hi"}).status_code == 403
    assert client.post(
        "/api/message",
        json={"text": "hi"},
        headers={"X-Web-Token": "wrong"},
    ).status_code == 403


def test_full_booking_requires_approval(client, token):
    first = client.post("/api/message", json={"user_id": "u1", "text": REQUEST},
                        headers=auth(token))
    assert first.status_code == 200
    body = first.json()
    assert body["outcome"] == "awaiting_approval"
    assert body["pending_approval"] is not None
    assert body["booking"] is None

    # Nothing is booked before the decision.
    bookings = client.get("/api/bookings?user_id=u1", headers=auth(token)).json()
    assert bookings["bookings"] == []

    second = client.post("/api/approve", json={"user_id": "u1", "decision": "yes"},
                         headers=auth(token))
    assert second.status_code == 200
    final = second.json()
    assert final["outcome"] == "booked"
    assert final["booking"] is not None
    assert final["pending_approval"] is None

    bookings = client.get("/api/bookings?user_id=u1", headers=auth(token)).json()
    assert len(bookings["bookings"]) == 1
    assert bookings["bookings"][0]["status"] == "confirmed"


def test_approve_without_pending_action_is_rejected(client, token):
    response = client.post("/api/approve", json={"user_id": "u1", "decision": "yes"},
                           headers=auth(token))
    assert response.status_code == 200
    assert response.json()["outcome"] == "no_pending_approval"


def test_cancel_decision_does_not_book(client, token):
    client.post("/api/message", json={"user_id": "u1", "text": REQUEST},
                headers=auth(token))
    response = client.post("/api/approve", json={"user_id": "u1", "decision": "no"},
                           headers=auth(token))
    assert response.status_code == 200
    assert response.json()["outcome"] in ("cancelled", "cancelled_by_user")
    bookings = client.get("/api/bookings?user_id=u1", headers=auth(token)).json()
    assert bookings["bookings"] == []


def test_trace_is_scoped_to_known_runs(client, token):
    client.post("/api/message", json={"user_id": "u1", "text": REQUEST},
                headers=auth(token))
    events = client.get("/api/trace?user_id=u1", headers=auth(token)).json()["events"]
    assert events
    assert all(event["run_id"] for event in events)
    assert any(event["event"] == "tool_call" for event in events)
    assert any(event["event"] == "state_transition" for event in events)

    other = client.get("/api/trace?user_id=u2", headers=auth(token)).json()["events"]
    assert other == []


def test_memory_inspect_and_forget(client, token):
    memories = client.get("/api/memories?user_id=u1", headers=auth(token)).json()
    facts = memories["memories"]
    assert len(facts) >= 50
    assert {fact["source"] for fact in facts} >= {"explicit", "inferred"}

    target = next(fact for fact in facts if fact["source"] == "explicit")
    response = client.post("/api/forget",
                           json={"user_id": "u1", "key": target["key"]},
                           headers=auth(token))
    assert response.status_code == 200
    assert response.json()["forgotten"] is True

    updated = client.get("/api/memories?user_id=u1", headers=auth(token)).json()
    match = next(fact for fact in updated["memories"] if fact["key"] == target["key"])
    assert match["forgotten"] == 1


def test_proactive_cancellation_requests_fresh_approval(client, token):
    client.post("/api/message", json={"user_id": "u1", "text": REQUEST},
                headers=auth(token))
    client.post("/api/approve", json={"user_id": "u1", "decision": "yes"},
                headers=auth(token))

    cancelled = client.post("/api/simulate-cancel", json={"user_id": "u1"},
                            headers=auth(token))
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert len(body["affected"]) == 1
    assert body["event"] is not None
    assert body["event"]["outcome"] == "proactive_awaiting_approval"
    assert body["event"]["pending_approval"] is not None

    # The replacement still needs a fresh, explicit approval.
    replacement = client.post("/api/approve",
                              json={"user_id": "u1", "decision": "yes"},
                              headers=auth(token)).json()
    assert replacement["outcome"] == "booked"

    bookings = client.get("/api/bookings?user_id=u1", headers=auth(token)).json()
    statuses = sorted(booking["status"] for booking in bookings["bookings"])
    assert statuses == ["confirmed", "provider_cancelled"]


def test_events_poll_is_idempotent(client, token):
    client.post("/api/message", json={"user_id": "u1", "text": REQUEST},
                headers=auth(token))
    client.post("/api/approve", json={"user_id": "u1", "decision": "yes"},
                headers=auth(token))
    client.post("/api/simulate-cancel", json={"user_id": "u1"}, headers=auth(token))

    # simulate-cancel already consumed the event, so a follow-up poll is empty.
    follow_up = client.post("/api/events", json={"user_id": "u1"},
                            headers=auth(token)).json()
    assert follow_up["event"] is None
