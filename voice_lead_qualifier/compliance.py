"""Illustrative pre-dial policy checks and post-call opt-out detection.

The 08:00-21:00 window and consent/DNC fields are test configuration, not a
statement of legal permission or a complete legal policy. This module does
not query a DNC registry, verify consent evidence, or place a phone call.
A real integration must apply the approved campaign and jurisdiction rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .models import Lead


@dataclass
class CallingPolicy:
    window_start: time = time(8, 0)
    window_end: time = time(21, 0)
    max_attempts: int = 3
    require_ai_voice_consent: bool = True
    blocked_weekdays: tuple[int, ...] = ()   # e.g. (6,) to skip Sundays


@dataclass
class GateDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def can_dial(lead: Lead, now_utc: datetime, policy: CallingPolicy | None = None) -> GateDecision:
    """Return whether the AI agent may place a call to this lead right now."""
    policy = policy or CallingPolicy()
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    reasons: list[str] = []

    if lead.on_internal_dnc:
        reasons.append("lead is on the internal do-not-call list")
    if lead.on_national_dnc:
        reasons.append("number is on the National Do Not Call Registry")
    if policy.require_ai_voice_consent and not lead.ai_voice_consent:
        reasons.append("no documented consent for AI-voice calls")
    if lead.attempts >= policy.max_attempts:
        reasons.append(f"attempt limit reached ({lead.attempts}/{policy.max_attempts})")

    local = now_utc.astimezone(ZoneInfo(lead.timezone))
    if not (policy.window_start <= local.time() < policy.window_end):
        reasons.append(f"outside calling window at lead local time {local:%H:%M}")
    if local.weekday() in policy.blocked_weekdays:
        reasons.append("blocked weekday at lead local time")

    return GateDecision(allowed=not reasons, reasons=reasons)


# Phrases that must end the call and suppress all future dials.
_OPT_OUT_PATTERNS = [
    r"\b(stop|quit) calling\b",
    r"\bdon'?t (ever )?call\b",
    r"\bdo not call\b",
    r"\btake me off\b",
    r"\bremove (me|my number)\b",
    r"\bput me on (your|the) do[- ]not[- ]call\b",
    r"\bunsubscribe\b",
    r"\bnever call\b",
]
_OPT_OUT_RE = re.compile("|".join(_OPT_OUT_PATTERNS), re.IGNORECASE)


def is_opt_out(utterance: str) -> bool:
    """True when a seller utterance is a request to stop calling."""
    return bool(_OPT_OUT_RE.search(utterance or ""))
