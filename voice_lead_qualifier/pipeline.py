"""Post-call webhook handler: verify -> normalise -> qualify -> write CRM.

Voice platforms send a webhook when a call ends. This module verifies the
HMAC signature, ignores duplicates (platforms retry webhooks), qualifies the
transcript and writes status, score, summary and transcript to the CRM, plus a
follow-up task where appropriate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from .crm import CRMClient
from .models import CallEvent, Lead, LeadStatus, Turn
from .qualification import qualify


class SignatureError(Exception):
    pass


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str) -> None:
    if not signature or not hmac.compare_digest(sign(body, secret), signature):
        raise SignatureError("webhook signature mismatch")


def parse_event(body: bytes) -> CallEvent:
    data = json.loads(body)
    return CallEvent(
        call_id=data["call_id"],
        lead_id=data["lead_id"],
        ended_at=datetime.fromisoformat(data["ended_at"]),
        duration_s=int(data.get("duration_s", 0)),
        answered_by=data.get("answered_by", "unknown"),
        turns=[Turn(t["speaker"], t["text"]) for t in data.get("turns", [])],
        transferred=bool(data.get("transferred", False)),
    )


FOLLOW_UP_DELAY = {
    LeadStatus.HOT: timedelta(hours=1),            # if the live transfer did not connect
    LeadStatus.WARM_FOLLOW_UP: timedelta(days=3),
    LeadStatus.NURTURE: timedelta(days=30),
    LeadStatus.VOICEMAIL: timedelta(days=2),
}


@dataclass
class Outcome:
    call_id: str
    status: LeadStatus
    score: int
    transfer: bool
    follow_up_due: datetime | None
    duplicate: bool = False


def summarise(event: CallEvent, q) -> str:
    parts = [f"Status {q.status.value}, score {q.score}."]
    if q.asking_price:
        parts.append(f"Asking ${q.asking_price:,}.")
    if q.timeline_months is not None:
        parts.append(f"Timeline ~{q.timeline_months:g} months.")
    if q.condition:
        parts.append(f"Condition {q.condition}.")
    if q.major_repairs is not None:
        parts.append("Major repairs reported." if q.major_repairs else "No major repairs reported.")
    return " ".join(parts)


def handle_post_call(body: bytes, signature: str, secret: str, lead: Lead, crm: CRMClient) -> Outcome:
    verify_signature(body, signature, secret)
    event = parse_event(body)
    if event.lead_id != lead.lead_id:
        raise ValueError("event lead_id does not match lead record")

    if crm.has_processed(event.call_id):
        return Outcome(event.call_id, LeadStatus(crm_status(crm, lead.lead_id)), 0, False, None, duplicate=True)

    q = qualify(event)
    transcript = "\n".join(f"{t.speaker}: {t.text}" for t in event.turns)

    fields = {
        "lead_status": q.status.value,
        "qualification_score": q.score,
        "last_call_id": event.call_id,
        "last_call_at": event.ended_at.isoformat(),
        "campaign": lead.campaign.value,
    }
    if q.asking_price is not None:
        fields["asking_price"] = q.asking_price
    if q.timeline_months is not None:
        fields["timeline_months"] = q.timeline_months
    if q.condition:
        fields["condition"] = q.condition
    if q.status is LeadStatus.DO_NOT_CALL:
        fields["do_not_call"] = True
        lead.on_internal_dnc = True

    crm.upsert_contact(lead.lead_id, fields)
    crm.add_note(lead.lead_id, summarise(event, q) + "\n\n" + transcript)
    crm.set_tags(lead.lead_id, [f"ai-call:{q.status.value}", f"campaign:{lead.campaign.value}"])

    due = None
    if q.status in FOLLOW_UP_DELAY and not (q.status is LeadStatus.HOT and event.transferred):
        due = event.ended_at + FOLLOW_UP_DELAY[q.status]
        crm.create_task(lead.lead_id, f"Follow up ({q.status.value}): {lead.property_address}", due)

    lead.attempts += 1
    crm.mark_processed(event.call_id)
    return Outcome(event.call_id, q.status, q.score, q.status is LeadStatus.HOT, due)


def crm_status(crm, lead_id: str) -> str:
    contacts = getattr(crm, "contacts", {})
    return contacts.get(lead_id, {}).get("lead_status", LeadStatus.NO_CONTACT.value)
