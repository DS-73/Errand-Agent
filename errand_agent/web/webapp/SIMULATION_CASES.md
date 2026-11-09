# Web Interface Simulation Cases

This document enumerates every simulation/test case exercised against the
FastAPI + React/Vite web layer. The source of truth for the automated cases is
`tests/test_web_api.py` (FastAPI `TestClient`). The live/server cases can be
reproduced against `http://127.0.0.1:8000` using `curl` or the browser UI.

All cases run against an in-memory SQLite database (`db=":memory:"`) with the
scripted model, seeded memories (63+ facts), and booking outcomes
`["success", "success"]`.

The base request used throughout:

```
POST /api/message
Headers: X-Web-Token: <token>, Content-Type: application/json
Body: {"user_id": "u1", "text": "Find a plumber in Indiranagar this week, mornings only, under ₹1,500."}
```

---

## 1. Session & authentication

### W-1-1 Health endpoint is public (no token)
| Step | Request | Expected |
|------|---------|----------|
| 1 | `GET /api/health` | `200` → `{"status": "ok"}` |

### W-1-2 Session returns token, user, model, limits, recovery
| Step | Request | Expected |
|------|---------|----------|
| 1 | `GET /api/session` | `200` → `{token, user_id: "u1", model: "scripted-policy-v1", limits: {...}, recovery: []}` |

### W-1-3 Protected routes reject missing token
| Step | Request | Expected |
|------|---------|----------|
| 1 | `GET /api/trace` | `403` |
| 2 | `POST /api/message` (no token) | `403` |
| 3 | `POST /api/message` with `X-Web-Token: wrong` | `403` |

### W-1-4 Protected routes accept valid token
| Step | Request | Expected |
|------|---------|----------|
| 1 | `GET /api/trace` with valid `X-Web-Token` | `200` |

---

## 2. Full booking flow with approval gate

### W-2-1 First message reaches approval, nothing booked yet
| Step | Request | Expected |
|------|---------|----------|
| 1 | `POST /api/message` with the base request | `200` → `outcome: "awaiting_approval"`, `pending_approval` not null, `booking: null` |
| 2 | `GET /api/bookings?user_id=u1` | `200` → `{"bookings": []}` |

### W-2-2 Approving confirms the booking
| Step | Request | Expected |
|------|---------|----------|
| 1 | `POST /api/approve` `{"user_id":"u1","decision":"yes"}` | `200` → `outcome: "booked"`, `booking` not null, `pending_approval: null` |
| 2 | `GET /api/bookings?user_id=u1` | `200` → exactly 1 booking, `status: "confirmed"` |
| 3 | Trace contains `tool_call` for `search_providers`, `get_provider`, `hold_slot`, `book` |

### W-2-3 Rejecting the approval books nothing
| Step | Request | Expected |
|------|---------|----------|
| 1 | `POST /api/message` (base request) → `awaiting_approval` |
| 2 | `POST /api/approve` `{"user_id":"u1","decision":"no"}` | `200` → `outcome` in `("cancelled", "cancelled_by_user")` |
| 3 | `GET /api/bookings?user_id=u1` | `[]` |

### W-2-4 Approve with no pending action
| Step | Request | Expected |
|------|---------|----------|
| 1 | `POST /api/approve` `{"user_id":"u1","decision":"yes"}` (fresh session) | `200` → `outcome: "no_pending_approval"`, `booking: null` |

---

## 3. Trace filtering

### W-3-1 Trace is scoped to the user's runs
| Step | Request | Expected |
|------|---------|----------|
| 1 | `POST /api/message` (base request) as `u1` | run created |
| 2 | `GET /api/trace?user_id=u1&limit=300` | `200` → `events` non-empty; every event has a `run_id` |
| 3 | `GET /api/trace?user_id=u2` | `200` → `{"events": []}` |

### W-3-2 Trace contains expected event kinds
| Step | Request | Expected |
|------|---------|----------|
| 1 | Run the base booking flow as `u1` |
| 2 | `GET /api/trace?user_id=u1` | Events include kinds: `run_start`, `user_message`, `intent`, `context`, `model_call`, `tool_call`, `state_transition`, `state_after_tool`, `outcome` |

---

## 4. Memory inspector

### W-4-1 Inspect returns seeded facts with provenance
| Step | Request | Expected |
|------|---------|----------|
| 1 | `GET /api/memories?user_id=u1` | `200` → `memories` length ≥ 50 |
| 2 | Sources include both `explicit` and `inferred` |
| 3 | Each fact has `id`, `user_id`, `key`, `value`, `source`, `confirmed`, `forgotten`, `created_at`, `updated_at` |

### W-4-2 Forgetting a fact marks it forgotten
| Step | Request | Expected |
|------|---------|----------|
| 1 | Pick an `explicit` fact from `/api/memories` |
| 2 | `POST /api/forget` `{"user_id":"u1","key":"<that key>"}` | `200` → `{"forgotten": true}` |
| 3 | `GET /api/memories?user_id=u1` | The fact now has `forgotten: 1` |

---

## 5. Proactive cancellation + replacement approval

### W-5-1 Simulated cancellation creates replacement pending approval
| Step | Request | Expected |
|------|---------|----------|
| 1 | Complete the full booking flow (`u1`) → `booked` |
| 2 | `POST /api/simulate-cancel` `{"user_id":"u1"}` | `200` → `affected` length 1, `event.outcome: "proactive_awaiting_approval"`, `event.pending_approval` not null |
| 3 | Original booking status is now `provider_cancelled` |

### W-5-2 Replacement still requires fresh approval
| Step | Request | Expected |
|------|---------|----------|
| 1 | After W-5-1, `POST /api/approve` `{"user_id":"u1","decision":"yes"}` | `200` → `outcome: "booked"` |
| 2 | `GET /api/bookings?user_id=u1` | Statuses: `["confirmed", "provider_cancelled"]` (one replacement confirmed, one cancelled) |

### W-5-3 Cancelling the replacement does not rebook
| Step | Request | Expected |
|------|---------|----------|
| 1 | After W-5-1, `POST /api/approve` `{"user_id":"u1","decision":"no"}` | `200` → replacement not booked |

### W-5-4 Events poll is idempotent (event consumed once)
| Step | Request | Expected |
|------|---------|----------|
| 1 | Complete booking → `simulate-cancel` (consumes the `provider_cancelled` event) |
| 2 | `POST /api/events` `{"user_id":"u1"}` | `200` → `{"event": null}` |

---

## 6. Server-side security invariants

### W-6-1 Browser cannot forge approval
| Step | Request | Expected |
|------|---------|----------|
| 1 | `POST /api/approve` with no pending gate and valid token | `outcome: "no_pending_approval"` (server refuses) |
| 2 | `POST /api/approve` with invalid/wrong token | `403` (gate never consulted) |

### W-6-2 Server binds to localhost only
| Check | Expected |
|-------|----------|
| `python3 -m webapp --host 127.0.0.1` | Listening on `127.0.0.1`, not `0.0.0.0` |

### W-6-3 CORS allows Vite dev ports
| Origin | Expected |
|--------|----------|
| `http://localhost:5173` | Allowed |
| `http://127.0.0.1:5173` | Allowed |
| `http://localhost:4173` | Allowed |
| `http://evil.example.com` | Not allowed (browser-enforced) |

---

## 7. Running the cases

### Automated (TestClient)
```bash
# From project root
python3 -m pytest tests/test_web_api.py -v
```
10 tests, all passing.

### Live server smoke test
```bash
# Terminal 1
make web-api          # uvicorn on :8000

# Terminal 2 (or browser)
make web-ui           # Vite dev on :5173, proxies /api → :8000
```

Or one-shot:
```bash
make web              # install deps, build, serve
```

Then open `http://localhost:5173` (dev) or `http://localhost:8000` (built).

### Manual curl against a live server
```bash
TOKEN=$(curl -s http://127.0.0.1:8000/api/session | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -s -X POST http://127.0.0.1:8000/api/message \
  -H "Content-Type: application/json" \
  -H "X-Web-Token: $TOKEN" \
  -d '{"user_id":"u1","text":"Find a plumber in Indiranagar this week, mornings only, under ₹1,500."}'

curl -s -X POST http://127.0.0.1:8000/api/approve \
  -H "Content-Type: application/json" \
  -H "X-Web-Token: $TOKEN" \
  -d '{"user_id":"u1","decision":"yes"}'
```

---

## 8. Case index

| ID | Area | Summary |
|----|------|---------|
| W-1-1 | Auth | `/api/health` is public |
| W-1-2 | Auth | `/api/session` returns token + limits |
| W-1-3 | Auth | Protected routes reject missing/wrong token |
| W-1-4 | Auth | Protected routes accept valid token |
| W-2-1 | Booking | First message → approval, nothing booked |
| W-2-2 | Booking | `yes` confirms booking |
| W-2-3 | Booking | `no` books nothing |
| W-2-4 | Booking | Approve with no pending action → `no_pending_approval` |
| W-3-1 | Trace | Per-user run scoping |
| W-3-2 | Trace | Expected event kinds present |
| W-4-1 | Memory | Inspect returns ≥50 facts with provenance |
| W-4-2 | Memory | Forget marks a fact forgotten |
| W-5-1 | Cancellation | Simulated cancellation → replacement pending approval |
| W-5-2 | Cancellation | Replacement needs fresh approval |
| W-5-3 | Cancellation | `no` on replacement → not rebooked |
| W-5-4 | Events | Poll is idempotent (event consumed once) |
| W-6-1 | Security | Server refuses forged/missing approval |
| W-6-2 | Security | Localhost-only bind |
| W-6-3 | Security | CORS allows Vite ports only |
