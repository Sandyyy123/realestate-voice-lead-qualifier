import json
from datetime import datetime, timezone

import pytest

from voice_lead_qualifier.compliance import CallingPolicy, can_dial, is_opt_out
from voice_lead_qualifier.crm import MockCRM
from voice_lead_qualifier.models import CallEvent, Campaign, Lead, LeadStatus, Turn
from voice_lead_qualifier.pipeline import SignatureError, handle_post_call, sign
from voice_lead_qualifier.qualification import qualify
from voice_lead_qualifier.reporting import build_report

SECRET = "test-secret-not-real"


def lead(**kw):
    base = dict(lead_id="L1", phone="+15555550100", owner_name="Pat Owner",
                property_address="12 Elm St", timezone="America/New_York",
                campaign=Campaign.WARM, ai_voice_consent=True, consent_source="web form 2026-09-01")
    base.update(kw)
    return Lead(**base)


def event(turns, answered_by="human", call_id="C1", transferred=False):
    return CallEvent(call_id=call_id, lead_id="L1",
                     ended_at=datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc),
                     duration_s=180, answered_by=answered_by,
                     turns=[Turn(s, t) for s, t in turns], transferred=transferred)


def body_of(ev):
    return json.dumps({
        "call_id": ev.call_id, "lead_id": ev.lead_id, "ended_at": ev.ended_at.isoformat(),
        "duration_s": ev.duration_s, "answered_by": ev.answered_by,
        "turns": [{"speaker": t.speaker, "text": t.text} for t in ev.turns],
        "transferred": ev.transferred,
    }).encode()


MOTIVATED = [
    ("agent", "Would you consider selling 12 Elm St?"),
    ("seller", "Yeah, I'd consider it."),
    ("agent", "How is the condition?"),
    ("seller", "Honestly it's in rough shape."),
    ("agent", "What price are you hoping for?"),
    ("seller", "Around 180k."),
    ("agent", "How soon?"),
    ("seller", "Within 2 months if the number works."),
    ("agent", "Any major repairs?"),
    ("seller", "The roof leaks and the foundation has cracks."),
]


# ---------- compliance gate ----------

def test_gate_allows_consented_lead_inside_window():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)   # 11:00 New York
    assert can_dial(lead(), now).allowed


def test_gate_blocks_outside_local_calling_window():
    now = datetime(2026, 10, 21, 1, 30, tzinfo=timezone.utc)   # 21:30 New York previous day
    d = can_dial(lead(), now)
    assert not d.allowed and any("calling window" in r for r in d.reasons)


def test_gate_uses_lead_timezone_not_server_time():
    now = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)  # 09:30 NY but 06:30 LA
    assert can_dial(lead(), now).allowed
    assert not can_dial(lead(timezone="America/Los_Angeles"), now).allowed


def test_gate_blocks_cold_lead_without_ai_voice_consent():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    d = can_dial(lead(campaign=Campaign.COLD, ai_voice_consent=False), now)
    assert not d.allowed and "no documented consent for AI-voice calls" in d.reasons


def test_gate_blocks_dnc_and_attempt_limit():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    d = can_dial(lead(on_national_dnc=True, attempts=3), now, CallingPolicy(max_attempts=3))
    assert len(d.reasons) == 2


def test_gate_rejects_naive_datetime():
    with pytest.raises(ValueError):
        can_dial(lead(), datetime(2026, 10, 20, 15, 0))


@pytest.mark.parametrize("text,expected", [
    ("Please stop calling me", True),
    ("Take me off your list", True),
    ("Put me on the do-not-call list", True),
    ("I'd stop by the house later", False),
    ("Call me back next week", False),
])
def test_opt_out_detection(text, expected):
    assert is_opt_out(text) is expected


# ---------- qualification state machine ----------

def test_motivated_seller_is_hot_with_all_answers():
    q = qualify(event(MOTIVATED))
    assert q.status is LeadStatus.HOT
    assert (q.interested, q.condition, q.asking_price, q.timeline_months, q.major_repairs) == \
        (True, "poor", 180_000, 2.0, True)
    assert q.questions_answered == 5 and q.score >= 70


def test_unclear_answer_keeps_state_until_answered():
    turns = [("seller", "Hmm, who is this?"), ("seller", "Sure, maybe"),
             ("seller", "It's okay, needs some work")]
    q = qualify(event(turns))
    assert q.interested is True and q.condition == "fair" and q.questions_answered == 2


def test_long_timeline_good_condition_is_not_hot():
    turns = [("seller", "Maybe"), ("seller", "It's in great shape, renovated"),
             ("seller", "I'd want 450,000"), ("seller", "No rush, maybe in 2 years"),
             ("seller", "No major repairs")]
    q = qualify(event(turns))
    assert q.status in (LeadStatus.WARM_FOLLOW_UP, LeadStatus.NURTURE)
    assert q.timeline_months == 24 and q.asking_price == 450_000


def test_not_interested_stops_questions():
    q = qualify(event([("seller", "No, not selling."), ("seller", "It's in rough shape")]))
    assert q.status is LeadStatus.NOT_INTERESTED and q.condition is None


def test_opt_out_mid_call_overrides_everything():
    turns = MOTIVATED[:4] + [("seller", "Actually, stop calling me.")]
    q = qualify(event(turns))
    assert q.status is LeadStatus.DO_NOT_CALL and q.opted_out


def test_voicemail_and_wrong_person():
    assert qualify(event([], answered_by="voicemail")).status is LeadStatus.VOICEMAIL
    assert qualify(event([("seller", "Wrong number, buddy")])).status is LeadStatus.NO_CONTACT


# ---------- webhook -> CRM ----------

def test_hot_lead_written_to_crm_with_transcript_and_no_task_when_transferred():
    crm, ld = MockCRM(), lead()
    ev = event(MOTIVATED, transferred=True)
    body = body_of(ev)
    out = handle_post_call(body, sign(body, SECRET), SECRET, ld, crm)
    assert out.status is LeadStatus.HOT and out.transfer
    c = crm.contacts["L1"]
    assert c["lead_status"] == "hot" and c["asking_price"] == 180_000 and c["qualification_score"] == out.score
    assert "seller: Around 180k." in crm.notes["L1"][0]
    assert crm.tasks == []           # live transfer connected, no callback needed
    assert ld.attempts == 1


def test_hot_lead_without_transfer_gets_callback_task():
    crm = MockCRM()
    ev = event(MOTIVATED, transferred=False)
    body = body_of(ev)
    out = handle_post_call(body, sign(body, SECRET), SECRET, lead(), crm)
    assert len(crm.tasks) == 1 and out.follow_up_due.hour == 16


def test_opt_out_sets_do_not_call_and_blocks_next_dial():
    crm, ld = MockCRM(), lead()
    ev = event([("seller", "Don't call me again.")])
    body = body_of(ev)
    handle_post_call(body, sign(body, SECRET), SECRET, ld, crm)
    assert crm.contacts["L1"]["do_not_call"] is True
    assert ld.on_internal_dnc
    later = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
    assert not can_dial(ld, later).allowed


def test_bad_signature_rejected_and_nothing_written():
    crm = MockCRM()
    body = body_of(event(MOTIVATED))
    with pytest.raises(SignatureError):
        handle_post_call(body, "deadbeef", SECRET, lead(), crm)
    assert crm.write_log == []


def test_webhook_retry_is_idempotent():
    crm, ld = MockCRM(), lead()
    body = body_of(event(MOTIVATED))
    first = handle_post_call(body, sign(body, SECRET), SECRET, ld, crm)
    writes = len(crm.write_log)
    second = handle_post_call(body, sign(body, SECRET), SECRET, ld, crm)
    assert second.duplicate and len(crm.write_log) == writes and ld.attempts == 1
    assert second.status is first.status


# ---------- reporting ----------

def test_report_counts_and_connect_rate():
    crm = MockCRM()
    outs = []
    cases = [(MOTIVATED, "human"), ([], "voicemail"), ([("seller", "No, not selling")], "human"),
             ([("seller", "stop calling")], "human")]
    for i, (turns, ans) in enumerate(cases):
        ev = event(turns, answered_by=ans, call_id=f"C{i}")
        body = body_of(ev)
        outs.append(handle_post_call(body, sign(body, SECRET), SECRET, lead(), crm))
    r = build_report(outs)
    assert (r.dials, r.connected, r.qualified, r.opt_outs) == (4, 3, 1, 1)
    assert r.connect_rate == 0.75
