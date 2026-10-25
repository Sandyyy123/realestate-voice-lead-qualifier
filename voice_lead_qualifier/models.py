"""Data objects shared by the post-call pipeline.

Everything here is plain dataclasses so the pipeline can be tested without a
voice platform, a phone line or a CRM account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Campaign(str, Enum):
    COLD = "cold"   # property owners with no recorded interest
    WARM = "warm"   # owners who previously asked to be contacted


class LeadStatus(str, Enum):
    HOT = "hot"                  # transfer now / book appointment
    WARM_FOLLOW_UP = "warm_follow_up"
    NURTURE = "nurture"          # long timeline, keep in drip
    NOT_INTERESTED = "not_interested"
    DO_NOT_CALL = "do_not_call"  # opt-out: never dial again
    VOICEMAIL = "voicemail"
    NO_CONTACT = "no_contact"    # wrong person / bad number


@dataclass
class Lead:
    lead_id: str
    phone: str
    owner_name: str
    property_address: str
    timezone: str                      # IANA name, e.g. "America/New_York"
    campaign: Campaign
    ai_voice_consent: bool = False     # supplied demo flag; legal sufficiency is not verified
    consent_source: Optional[str] = None
    on_national_dnc: bool = False
    on_internal_dnc: bool = False
    attempts: int = 0


@dataclass
class Turn:
    speaker: str   # "agent" or "seller"
    text: str


@dataclass
class CallEvent:
    """Custom example event shape; not a verified provider payload schema."""
    call_id: str
    lead_id: str
    ended_at: datetime
    duration_s: int
    answered_by: str                    # "human" | "voicemail" | "unknown"
    turns: list[Turn] = field(default_factory=list)
    transferred: bool = False


@dataclass
class Qualification:
    interested: Optional[bool] = None
    condition: Optional[str] = None      # "good" | "fair" | "poor"
    asking_price: Optional[int] = None
    timeline_months: Optional[float] = None
    major_repairs: Optional[bool] = None
    opted_out: bool = False
    wrong_person: bool = False
    questions_answered: int = 0
    score: int = 0
    status: LeadStatus = LeadStatus.NO_CONTACT
    reasons: list[str] = field(default_factory=list)
