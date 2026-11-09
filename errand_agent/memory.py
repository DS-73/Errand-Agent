"""Persistent memory with explicit/inferred provenance and forgetting."""
from __future__ import annotations

import re
import time
from typing import Any

from .mock_env import AREAS, SERVICES
from .models import Intent
from .store import Store

FORGET_KEY_ALIASES = {
    "preferred area": "preferred_area",
    "area": "preferred_area",
    "preferred time": "earliest_time",
    "earliest time": "earliest_time",
    "budget": "budget",
    "technician": "preferred_technician",
    "preferred technician": "preferred_technician",
}

ALWAYS_RELEVANT = {
    "preferred_area",
    "earliest_time",
    "mornings_only",
    "budget",
    "preferred_technician",
}


class MemoryStore:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ seeding
    def seed_demo_memories(self, user_id: str = "u1", now: float | None = None) -> int:
        """Seed 50+ facts, including forgotten and unconfirmed-inferred ones."""
        now = now if now is not None else time.time()
        explicit = {
            "preferred_area": "Indiranagar",
            "earliest_time": "10:00",
            "mornings_only": "true",
            "budget_plumber": "1500",
            "budget_electrician": "1200",
            "preferred_technician_electrician": "Ravi Home Services",
            "preferred_technician_plumber": "Suresh Repairs",
            "home_type": "2BHK apartment",
            "has_pets": "true",
            "gate_code": "4821",
            "preferred_language": "English",
            "contact_phone": "+91-90000-00000",
            "address_line": "12, 4th Cross, Indiranagar",
            "parking": "street parking available",
            "water_source": "borewell + municipal",
            "lift_available": "true",
            "floor": "3",
            "preferred_payment": "UPI",
            "invoice_email": "demo@example.com",
        }
        inferred = {
            "inferred_area": "Koramangala",
            "inferred_service": "plumber",
            "inferred_budget": "2000",
        }
        noisy = {
            "favourite_colour": "blue",
            "pet_name": "Coco",
            "favourite_food": "dosa",
            "car_model": "Hyundai i20",
            "office_area": "MG Road",
            "gym": "Cult Fit",
            "coffee_order": "flat white",
            "birthday_month": "March",
            "kids_school": "Greenwood High",
            "wifi_name": "HomeNet-5G",
            "newspaper": "The Hindu",
            "laundry_day": "Sunday",
            "milk_brand": "Nandini",
            "plant_count": "14",
            "balcony_garden": "true",
            "favourite_show": "The Office",
            "sports_team": "RCB",
            "music_genre": "indie",
            "shoe_size": "9",
            "tshirt_size": "M",
            "blood_group": "O+",
            "emergency_contact": "Anita",
            "landlord_name": "Mr. Rao",
            "lease_end": "2027-05-31",
            "scooter_model": "Activa",
            "commute_mode": "metro",
            "favourite_restaurant": "Truffles",
            "weekend_plan": "hiking",
            "book_club": "Wednesday",
            "haircut_place": "Bounce",
            "dentist": "Dr. Shah",
            "optician": "Lenskart",
            "bank": "HDFC",
            "courier_preference": "Blue Dart",
            "grocery_app": "BigBasket",
            "electricity_provider": "BESCOM",
            "internet_provider": "ACT",
            "gas_connection": "piped",
            "society_name": "Palm Meadows",
            "security_guard": "Ramesh",
        }
        for key, value in explicit.items():
            self.store.upsert_memory(user_id, key, value, source="explicit", confirmed=True, now=now)
        for key, value in inferred.items():
            self.store.upsert_memory(user_id, key, value, source="inferred", confirmed=False, now=now)
        for key, value in noisy.items():
            self.store.upsert_memory(user_id, key, value, source="explicit", confirmed=True, now=now)
        # A forgotten fact that must never reach the model.
        self.store.upsert_memory(user_id, "old_area", "Whitefield", source="explicit",
                                 confirmed=True, now=now)
        self.store.forget_memory(user_id, "old_area", now=now)
        return len(self.store.list_memories(user_id, include_forgotten=True))

    # -------------------------------------------------------------- extraction
    def handle_user_statement(self, user_id: str, text: str,
                              now: float | None = None) -> list[str]:
        """Apply explicit preference statements, corrections, and forgetting.

        Returns a list of human-readable changes for the trace.
        """
        now = now if now is not None else time.time()
        changes: list[str] = []
        low = text.lower()

        # Forgetting
        m = re.search(r"\bforget\b(?:\s+(?:my|the|about))?\s+(.+?)[.!?]*$", text, re.IGNORECASE)
        if m:
            phrase = m.group(1).strip().lower()
            key = FORGET_KEY_ALIASES.get(phrase)
            if key is None:
                for alias, candidate in FORGET_KEY_ALIASES.items():
                    if alias in phrase:
                        key = candidate
                        break
            if key is None:
                # "forget my preferred area" / "forget area"
                if "area" in phrase:
                    key = "preferred_area"
                elif "time" in phrase:
                    key = "earliest_time"
                elif "technician" in phrase:
                    key = "preferred_technician"
            if key:
                if self.store.forget_memory(user_id, key, now=now):
                    changes.append(f"forgot:{key}")
                else:
                    changes.append(f"forget_missing:{key}")
            return changes

        # Area (explicit or correction)
        area_match = re.search(
            r"(?:my area is|i live in|i'?m in|i am in|prefer(?:red)? area(?: is)?|"
            r"area is|move(?:d)? to)\s+([A-Za-z][A-Za-z ]{2,30}?)(?:[.,]|$)",
            text, re.IGNORECASE,
        )
        if area_match:
            candidate = area_match.group(1).strip()
            matched = next((a for a in AREAS if a.lower() in candidate.lower()), None)
            if matched:
                self.store.correct_memory(user_id, "preferred_area", matched, now=now)
                changes.append(f"set:preferred_area={matched}")

        # Earliest time
        time_match = re.search(
            r"(?:not before|earliest(?: at)?|no earlier than|from)\s*"
            r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?",
            low,
        )
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            ampm = (time_match.group(3) or "").replace(".", "")
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            if not ampm and 1 <= hour <= 7:
                hour += 12
            value = f"{hour:02d}:{minute:02d}"
            self.store.correct_memory(user_id, "earliest_time", value, now=now)
            changes.append(f"set:earliest_time={value}")

        # Mornings only
        if re.search(r"\bmornings? only\b", low):
            self.store.correct_memory(user_id, "mornings_only", "true", now=now)
            changes.append("set:mornings_only=true")

        # Budget, optionally scoped to a service
        budget_match = re.search(
            r"(?:budget(?: of| is)?|under|below|less than|upto|up to)\s*[₹rs.]*\s*([\d,]+)",
            low,
        )
        if budget_match:
            value = budget_match.group(1).replace(",", "")
            service = next((s for s in SERVICES if s.replace("_", " ") in low), None)
            key = f"budget_{service}" if service else "budget"
            self.store.correct_memory(user_id, key, value, now=now)
            changes.append(f"set:{key}={value}")

        # Technician preference: "I prefer Ravi" / "same electrician as last time"
        tech_match = re.search(
            r"(?:prefer|same)\s+(?:the\s+)?(?:plumber|electrician|technician|carpenter|painter)?\s*"
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
            text,
        )
        if tech_match and "last time" not in low:
            service = next((s for s in SERVICES if s.replace("_", " ") in low), None)
            key = f"preferred_technician_{service}" if service else "preferred_technician"
            value = tech_match.group(1).strip()
            self.store.correct_memory(user_id, key, value, now=now)
            changes.append(f"set:{key}={value}")

        return changes

    # --------------------------------------------------------------- inference
    def record_booking_inferences(self, user_id: str, booking: dict[str, Any],
                                  now: float | None = None) -> None:
        """Store inferred facts as *unconfirmed* so they are not injected into
        the model context until the user explicitly confirms them."""
        now = now if now is not None else time.time()
        inferred = {
            "inferred_area": booking.get("area"),
            "inferred_service": booking.get("service"),
            "inferred_technician": booking.get("provider_name"),
        }
        for key, value in inferred.items():
            if not value:
                continue
            existing = self.store.get_memory(user_id, key)
            if existing and existing["source"] == "explicit" and not existing["forgotten"]:
                continue
            self.store.upsert_memory(user_id, key, str(value), source="inferred",
                                     confirmed=False, now=now)

    def confirm(self, user_id: str, key: str, now: float | None = None) -> bool:
        return self.store.confirm_memory(user_id, key, now=now)

    # --------------------------------------------------------------- retrieval
    def relevant(self, user_id: str, intent: Intent | None,
                 limit: int = 8) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (included, excluded_with_reasons) for context construction."""
        memories = self.store.list_memories(user_id, include_forgotten=True)
        service = (intent.service if intent else None) or ""
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for mem in memories:
            key = mem["key"]
            if mem["forgotten"]:
                excluded.append({**mem, "reason": "forgotten"})
                continue
            if mem["source"] == "inferred" and not mem["confirmed"]:
                excluded.append({**mem, "reason": "unconfirmed_inferred"})
                continue
            relevant = key in ALWAYS_RELEVANT
            if key.startswith("budget_") and service and key == f"budget_{service}":
                relevant = True
            if key.startswith("preferred_technician_") and service and \
                    key == f"preferred_technician_{service}":
                relevant = True
            if not relevant:
                excluded.append({**mem, "reason": "irrelevant"})
                continue
            included.append(mem)
        # Stable, preference-first ordering.
        order = {"preferred_area": 0, "earliest_time": 1, "mornings_only": 2}
        included.sort(key=lambda m: (order.get(m["key"], 5), m["id"]))
        if len(included) > limit:
            excluded.extend({**m, "reason": "context_limit"} for m in included[limit:])
            included = included[:limit]
        return included, excluded

    def format_lines(self, memories: list[dict[str, Any]]) -> str:
        if not memories:
            return "(no stored preferences)"
        lines = []
        for mem in memories:
            tag = "explicit" if mem["source"] == "explicit" else "inferred/confirmed"
            lines.append(f"- {mem['key']}: {mem['value']} ({tag})")
        return "\n".join(lines)

    def inspect(self, user_id: str) -> list[dict[str, Any]]:
        return self.store.list_memories(user_id, include_forgotten=True)
