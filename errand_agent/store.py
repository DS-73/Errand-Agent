"""SQLite-backed persistence for memory, holds, bookings, and operations.

Using a real database (rather than in-process dicts) is what makes the
"restart mid-booking" scenario meaningful: a new orchestrator can reopen the
same file and resolve an operation that was left in flight.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'explicit',
    confirmed INTEGER NOT NULL DEFAULT 1,
    forgotten INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(user_id, key)
);
CREATE TABLE IF NOT EXISTS holds (
    hold_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    price INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_name TEXT,
    service TEXT NOT NULL,
    slot TEXT NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    hold_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    booking_id TEXT,
    outcome TEXT NOT NULL,
    detail TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    idempotency_key TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    hold_id TEXT,
    booking_id TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_attempts (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ memory
    def upsert_memory(self, user_id: str, key: str, value: str, source: str = "explicit",
                      confirmed: bool = True, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO memories(user_id,key,value,source,confirmed,forgotten,created_at,updated_at)
                   VALUES(?,?,?,?,?,0,?,?)
                   ON CONFLICT(user_id,key) DO UPDATE SET
                     value=excluded.value, source=excluded.source,
                     confirmed=excluded.confirmed, forgotten=0, updated_at=excluded.updated_at""",
                (user_id, key, value, source, int(confirmed), now, now),
            )
            self._conn.commit()

    def get_memory(self, user_id: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE user_id=? AND key=?", (user_id, key)
            ).fetchone()
        return dict(row) if row else None

    def list_memories(self, user_id: str, include_forgotten: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM memories WHERE user_id=?"
        if not include_forgotten:
            sql += " AND forgotten=0"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def forget_memory(self, user_id: str, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET forgotten=1, updated_at=? WHERE user_id=? AND key=?",
                (now, user_id, key),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def correct_memory(self, user_id: str, key: str, value: str, now: float | None = None) -> None:
        self.upsert_memory(user_id, key, value, source="explicit", confirmed=True, now=now)

    def confirm_memory(self, user_id: str, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET confirmed=1, updated_at=? WHERE user_id=? AND key=? AND forgotten=0",
                (now, user_id, key),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------- holds
    def insert_hold(self, hold: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO holds(hold_id,user_id,provider_id,slot,price,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (hold["hold_id"], hold["user_id"], hold["provider_id"], hold["slot"],
                 hold["price"], hold["expires_at"], hold["created_at"]),
            )
            self._conn.commit()

    def get_hold(self, hold_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        return dict(row) if row else None

    def update_hold_price(self, hold_id: str, price: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE holds SET price=? WHERE hold_id=?", (price, hold_id))
            self._conn.commit()

    def delete_hold(self, hold_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM holds WHERE hold_id=?", (hold_id,))
            self._conn.commit()

    # ----------------------------------------------------------------- bookings
    def insert_booking(self, booking: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO bookings(
                     booking_id,user_id,provider_id,provider_name,service,slot,price,status,
                     idempotency_key,hold_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (booking["booking_id"], booking["user_id"], booking["provider_id"],
                 booking.get("provider_name"), booking["service"], booking["slot"],
                 booking["price"], booking["status"], booking.get("idempotency_key"),
                 booking.get("hold_id"), booking["created_at"], booking["updated_at"]),
            )
            self._conn.commit()

    def get_booking(self, booking_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM bookings WHERE booking_id=?", (booking_id,)).fetchone()
        return dict(row) if row else None

    def get_booking_by_key(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM bookings WHERE idempotency_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def get_booking_by_hold(self, hold_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM bookings WHERE hold_id=?", (hold_id,)).fetchone()
        return dict(row) if row else None

    def update_booking(self, booking_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = fields.get("updated_at", time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [booking_id]
        with self._lock:
            self._conn.execute(f"UPDATE bookings SET {cols} WHERE booking_id=?", values)
            self._conn.commit()

    def list_bookings(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bookings WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- idempotency
    def get_idem(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def set_idem(self, key: str, outcome: str, booking_id: str | None = None,
                 detail: str | None = None, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO idempotency(key,booking_id,outcome,detail,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET booking_id=excluded.booking_id,
                     outcome=excluded.outcome, detail=excluded.detail, updated_at=excluded.updated_at""",
                (key, booking_id, outcome, detail, now),
            )
            self._conn.commit()

    # --------------------------------------------------------------- operations
    def upsert_operation(self, key: str, user_id: str, action: str, fingerprint: str,
                         status: str, hold_id: str | None = None,
                         booking_id: str | None = None, detail: str | None = None,
                         now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO operations(idempotency_key,user_id,action,fingerprint,hold_id,
                     booking_id,status,detail,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(idempotency_key) DO UPDATE SET
                     hold_id=excluded.hold_id, booking_id=excluded.booking_id,
                     status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at""",
                (key, user_id, action, fingerprint, hold_id, booking_id, status, detail, now),
            )
            self._conn.commit()

    def get_operation(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operations WHERE idempotency_key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def list_unresolved_operations(self, user_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM operations WHERE status IN ('pending','timeout')"
        args: tuple[Any, ...] = ()
        if user_id:
            sql += " AND user_id=?"
            args = (user_id,)
        sql += " ORDER BY updated_at"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- events
    def add_event(self, user_id: str, kind: str, payload: dict[str, Any],
                  now: float | None = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(user_id,kind,payload,consumed,created_at) VALUES(?,?,?,0,?)",
                (user_id, kind, json.dumps(payload), now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_events(self, user_id: str, unconsumed_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE user_id=?"
        if unconsumed_only:
            sql += " AND consumed=0"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, (user_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def mark_event_consumed(self, event_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE events SET consumed=1 WHERE id=?", (event_id,))
            self._conn.commit()

    # ------------------------------------------------------------ tool attempts
    def bump_attempt(self, key: str, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO tool_attempts(key,count,updated_at) VALUES(?,1,?)
                   ON CONFLICT(key) DO UPDATE SET count=count+1, updated_at=excluded.updated_at""",
                (key, now),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT count FROM tool_attempts WHERE key=?", (key,)).fetchone()
        return int(row["count"]) if row else 1

    def get_attempts(self, key: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT count FROM tool_attempts WHERE key=?", (key,)).fetchone()
        return int(row["count"]) if row else 0
