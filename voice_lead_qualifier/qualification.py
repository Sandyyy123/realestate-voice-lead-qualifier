"""Rule-based seller qualification over a finished call transcript.

In production the live conversation is run by the voice platform's LLM agent.
This module is the deterministic layer that runs AFTER the call: it walks the
five qualification questions in order, extracts the seller's answers, scores
the lead and assigns a CRM status. Deterministic rules make the routing
auditable and testable; the platform's own structured extraction can replace
the extractors without changing the scoring or status logic.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from .compliance import is_opt_out
from .models import CallEvent, LeadStatus, Qualification


class Step(str, Enum):
    INTEREST = "interest"
    CONDITION = "condition"
    PRICE = "price"
    TIMELINE = "timeline"
    REPAIRS = "repairs"
    DONE = "done"


ORDER = [Step.INTEREST, Step.CONDITION, Step.PRICE, Step.TIMELINE, Step.REPAIRS, Step.DONE]

_YES = re.compile(r"\b(yes|yeah|yep|sure|maybe|possibly|open to it|would consider|thinking about it|depends)\b", re.I)
_NO = re.compile(r"\b(no|nope|not interested|not selling|never selling|not for sale)\b", re.I)
_WRONG = re.compile(r"\b(wrong number|don'?t own|not the owner|sold it already|no one by that name)\b", re.I)


def _interest(text: str) -> Optional[bool]:
    if _NO.search(text) and not re.search(r"\bnot (sure|opposed)\b", text, re.I):
        return False
    if _YES.search(text):
        return True
    return None


def _condition(text: str) -> Optional[str]:
    t = text.lower()
    if re.search(r"\b(rough|poor|bad shape|needs a lot|gutted|fire damage|vacant and|run[- ]down|fixer)\b", t):
        return "poor"
    if re.search(r"\b(ok|okay|fair|average|needs some|dated|some work)\b", t):
        return "fair"
    if re.search(r"\b(good|great|excellent|updated|renovated|move[- ]in ready)\b", t):
        return "good"
    return None


_NUM = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|thousand|m|million)?", re.I)


def _price(text: str) -> Optional[int]:
    best = None
    for m in _NUM.finditer(text):
        raw, unit = m.group(1).replace(",", ""), (m.group(2) or "").lower()
        value = float(raw)
        if unit in ("k", "thousand"):
            value *= 1_000
        elif unit in ("m", "million"):
            value *= 1_000_000
        if value >= 10_000:            # ignore small numbers such as "2 months"
            best = int(value)
    return best


_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a": 1, "a couple": 2, "a few": 3}


def _timeline(text: str) -> Optional[float]:
    t = text.lower()
    if re.search(r"\b(asap|right away|immediately|this month|as soon as)\b", t):
        return 0.5
    m = re.search(r"\b(\d+|one|two|three|four|five|six|a couple|a few|a)\s+(week|month|year)s?\b", t)
    if m:
        n = float(_WORDNUM.get(m.group(1), m.group(1)))
        return {"week": n / 4.0, "month": n, "year": n * 12}[m.group(2)]
    if re.search(r"\b(no rush|someday|not anytime soon|eventually)\b", t):
        return 24.0
    return None


def _repairs(text: str) -> Optional[bool]:
    t = text.lower()
    if re.search(r"\b(no (major )?repairs|nothing major|no issues|no problems)\b", t):
        return False
    if re.search(r"\b(roof|foundation|mold|leak|plumbing|electrical|hvac|termite|water damage|repairs?)\b", t):
        return True
    return None


_EXTRACTORS = {
    Step.INTEREST: _interest,
    Step.CONDITION: _condition,
    Step.PRICE: _price,
    Step.TIMELINE: _timeline,
    Step.REPAIRS: _repairs,
}
_FIELDS = {
    Step.INTEREST: "interested",
    Step.CONDITION: "condition",
    Step.PRICE: "asking_price",
    Step.TIMELINE: "timeline_months",
    Step.REPAIRS: "major_repairs",
}


def qualify(event: CallEvent, hot_threshold: int = 70, warm_threshold: int = 40) -> Qualification:
    """Walk the transcript as a state machine and return the qualification."""
    q = Qualification()

    if event.answered_by == "voicemail":
        q.status = LeadStatus.VOICEMAIL
        q.reasons.append("voicemail detected")
        return q

    step_idx = 0
    for turn in event.turns:
        if turn.speaker != "seller":
            continue
        if is_opt_out(turn.text):
            q.opted_out = True
            q.status = LeadStatus.DO_NOT_CALL
            q.reasons.append("seller asked not to be called")
            return q
        if _WRONG.search(turn.text):
            q.wrong_person = True
            q.status = LeadStatus.NO_CONTACT
            q.reasons.append("wrong person or not the owner")
            return q
        step = ORDER[step_idx]
        if step is Step.DONE:
            continue
        value = _EXTRACTORS[step](turn.text)
        if value is None:
            continue   # agent re-asks; answer still pending, stay on this step
        setattr(q, _FIELDS[step], value)
        q.questions_answered += 1
        if step is Step.INTEREST and value is False:
            q.status = LeadStatus.NOT_INTERESTED
            q.reasons.append("seller not interested")
            return q
        step_idx += 1

    q.score = score(q)
    if q.interested is None:
        q.status = LeadStatus.NO_CONTACT
        q.reasons.append("interest never established")
    elif q.score >= hot_threshold:
        q.status = LeadStatus.HOT
    elif q.score >= warm_threshold:
        q.status = LeadStatus.WARM_FOLLOW_UP
    else:
        q.status = LeadStatus.NURTURE
    return q


def score(q: Qualification) -> int:
    """0-100 motivation score. Weights are a starting point to tune on real calls."""
    s = 0
    if q.interested:
        s += 30
        q.reasons.append("open to selling (+30)")
    if q.timeline_months is not None:
        if q.timeline_months <= 3:
            s += 25; q.reasons.append("timeline <= 3 months (+25)")
        elif q.timeline_months <= 6:
            s += 15; q.reasons.append("timeline <= 6 months (+15)")
        elif q.timeline_months <= 12:
            s += 5; q.reasons.append("timeline <= 12 months (+5)")
    if q.condition == "poor":
        s += 15; q.reasons.append("poor condition (+15)")
    elif q.condition == "fair":
        s += 8; q.reasons.append("fair condition (+8)")
    if q.major_repairs:
        s += 10; q.reasons.append("major repairs (+10)")
    if q.asking_price is not None:
        s += 10; q.reasons.append("gave a price (+10)")
    s += 2 * q.questions_answered   # engagement, max +10
    return min(s, 100)
