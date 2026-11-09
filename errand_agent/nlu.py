"""Deterministic natural-language understanding.

This is the offline stand-in for the LLM's intent extraction.  The real
OpenAI-compatible adapter in ``llm.py`` can replace it, but the rest of the
system (workflow, approval gate, executor) is identical either way.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .mock_env import AREAS, SERVICES
from .models import Intent

SERVICE_ALIASES = {
    "plumber": ["plumber", "plumbing", "tap", "leak", "pipe", "drain"],
    "electrician": ["electrician", "electrical", "wiring", "switch", "socket", "light fitting"],
    "ac_servicing": ["ac servicing", "ac service", "air conditioner", "air conditioning", "ac "],
    "carpenter": ["carpenter", "carpentry", "furniture", "door repair", "wardrobe"],
    "painter": ["painter", "painting", "paint the", "wall paint"],
}

AFFIRMATIVE_RE = re.compile(
    r"\b(yes|yeah|yep|yup|ok|okay|sure|confirm(?:ed)?|proceed|approve[d]?|"
    r"go ahead|do it|book it|please do|sounds good|that works)\b",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"\b(no|nope|nah|don'?t|do not|stop|wait|hold on|not yet|cancel that|"
    r"never mind|nevermind|reject)\b",
    re.IGNORECASE,
)
CHANGE_RE = re.compile(
    r"\b(actually|instead|change|different|rather|make it|move it|switch to|"
    r"not .* but|update)\b",
    re.IGNORECASE,
)
FORGET_RE = re.compile(r"\bforget\b(?:\s+(?:my|the|about))?\s+(.+?)[.!?]*$", re.IGNORECASE)


def is_affirmative(text: str) -> bool:
    return bool(AFFIRMATIVE_RE.search(text or "")) and not bool(NEGATION_RE.search(text or ""))


def is_negation(text: str) -> bool:
    return bool(NEGATION_RE.search(text or ""))


def has_change(text: str) -> bool:
    return bool(CHANGE_RE.search(text or ""))


def _parse_date_range(text: str, today: date) -> tuple[str | None, str | None, str | None]:
    """Return (start, end, target_date)."""
    low = text.lower()
    if "today" in low:
        return today.isoformat(), today.isoformat(), today.isoformat()
    if "tomorrow" in low:
        d = today + timedelta(days=1)
        return d.isoformat(), d.isoformat(), d.isoformat()
    if "this week" in low:
        return today.isoformat(), (today + timedelta(days=6)).isoformat(), None
    if "next week" in low:
        monday = today + timedelta(days=(7 - today.weekday()))
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat(), None

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, name in enumerate(weekdays):
        if re.search(rf"\b{name}\b", low):
            delta = (i - today.weekday()) % 7
            if delta == 0:
                delta = 7
            d = today + timedelta(days=delta)
            return d.isoformat(), d.isoformat(), d.isoformat()
    return None, None, None


def _parse_budget(text: str) -> int | None:
    patterns = [
        r"(?:under|below|less than|upto|up to|max(?:imum)?|budget(?: of| is)?)\s*[₹rs.]*\s*([\d,]+)",
        r"₹\s*([\d,]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def _parse_time(text: str) -> str | None:
    low = text.lower()
    # "not before 10 a.m.", "after 10am", "earliest 10", "no earlier than 10"
    m = re.search(
        r"(?:not before|after|earliest(?: at)?|no earlier than|from)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?",
        low,
    )
    if not m:
        # "make it 3pm", "at 3 pm", "3pm"
        m = re.search(r"\b(?:at\s+|make it\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", low)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not ampm and 1 <= hour <= 7:
        # "after 4" in a service context almost always means afternoon.
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def _parse_service(text: str) -> str | None:
    low = text.lower()
    # Prefer the longest alias to avoid "ac " matching inside words.
    best: tuple[int, str] | None = None
    for service, aliases in SERVICE_ALIASES.items():
        for alias in aliases:
            if alias in low:
                if best is None or len(alias) > best[0]:
                    best = (len(alias), service)
    return best[1] if best else None


def _parse_area(text: str, memories: list[dict] | None) -> str | None:
    low = text.lower()
    for area in AREAS:
        if area.lower() in low:
            return area
    # Accept a few common spellings.
    aliases = {"hsr": "HSR Layout", "white field": "Whitefield", "jayanagar": "Jayanagar"}
    for alias, area in aliases.items():
        if alias in low:
            return area
    if memories:
        for mem in memories:
            if mem.get("key") == "preferred_area" and not mem.get("forgotten") \
                    and mem.get("source") == "explicit":
                return mem.get("value")
    return None


def _parse_action(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(cancel|call off|drop)\b", low):
        return "cancel"
    if re.search(r"\b(move|reschedule|change .* to|shift)\b", low):
        return "reschedule"
    if re.search(r"\b(list|show|what are)\b.*\bbookings?\b", low):
        return "list"
    return "book"


def parse_intent(text: str, memories: list[dict] | None = None,
                 today: date | None = None) -> Intent:
    today = today or date.today()
    low = text.lower()
    action = _parse_action(text)
    start, end, target = _parse_date_range(text, today)
    service = _parse_service(text)
    area = _parse_area(text, memories)
    budget = _parse_budget(text)
    earliest = _parse_time(text)
    mornings = bool(re.search(r"\bmornings?\b|\bbefore noon\b", low))
    same_as_last = bool(re.search(r"\bsame\b.*\bas last time\b|\blast time\b", low))
    technician = None
    if same_as_last and memories:
        for mem in memories:
            if mem.get("key", "").startswith("preferred_technician") and not mem.get("forgotten"):
                technician = mem.get("value")
                break
    target_slot = None
    # "to Saturday at 10" -> date + time
    if target and action == "reschedule":
        target_slot = f"{target}T{earliest}" if earliest else target
    return Intent(
        action=action,
        service=service,
        area=area,
        date_start=start,
        date_end=end,
        budget=budget,
        mornings_only=mornings,
        earliest_time=earliest,
        preferred_technician=technician,
        target_slot=target_slot,
        same_as_last=same_as_last,
        raw=text,
    )


def missing_prompt(intent: Intent) -> str:
    missing = intent.missing()
    if not missing:
        return ""
    labels = {
        "service": "which service you need (plumber, electrician, AC servicing, carpenter, painter)",
        "area": "the area you're in",
        "date_range": "which day or date range works for you",
    }
    if len(missing) == 1:
        return f"Sure — what's {labels[missing[0]]}?"
    parts = [labels[m] for m in missing]
    return "I can help with that. Could you tell me " + ", ".join(parts[:-1]) + " and " + parts[-1] + "?"


def describe_intent(intent: Intent) -> str:
    bits = []
    if intent.service:
        bits.append(intent.service.replace("_", " "))
    if intent.area:
        bits.append(f"in {intent.area}")
    if intent.date_start:
        bits.append(f"on {intent.date_start}" if intent.date_start == intent.date_end
                    else f"between {intent.date_start} and {intent.date_end}")
    if intent.mornings_only:
        bits.append("mornings only")
    if intent.earliest_time:
        bits.append(f"not before {intent.earliest_time}")
    if intent.budget:
        bits.append(f"under ₹{intent.budget:,}")
    return ", ".join(bits) if bits else "your errand"
