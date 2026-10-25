"""Run the post-call pipeline over constructed example calls (no real calls, no real CRM).

Usage: python main.py [path/to/synthetic_calls.jsonl]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from voice_lead_qualifier.compliance import can_dial
from voice_lead_qualifier.crm import MockCRM
from voice_lead_qualifier.models import Campaign, Lead
from voice_lead_qualifier.pipeline import handle_post_call, sign
from voice_lead_qualifier.reporting import build_report

DEMO_SECRET = "demo-secret-not-real"


def main(path: Path) -> int:
    crm = MockCRM()
    outcomes = []
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    print(f"{'lead':<5} {'campaign':<8} {'gate':<9} {'status':<15} {'score':>5}  follow-up")
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        lead = Lead(**{**rec["lead"], "campaign": Campaign(rec["lead"]["campaign"])})
        gate = can_dial(lead, now)
        if not gate.allowed:
            print(f"{lead.lead_id:<5} {lead.campaign.value:<8} {'BLOCKED':<9} {'-':<15} {'-':>5}  {'; '.join(gate.reasons)}")
            continue
        body = json.dumps(rec["call"]).encode()
        out = handle_post_call(body, sign(body, DEMO_SECRET), DEMO_SECRET, lead, crm)
        outcomes.append(out)
        due = out.follow_up_due.isoformat(timespec="minutes") if out.follow_up_due else "-"
        print(f"{lead.lead_id:<5} {lead.campaign.value:<8} {'dialled':<9} {out.status.value:<15} {out.score:>5}  {due}")
    r = build_report(outcomes)
    print(f"\nReport: dials={r.dials} connected={r.connected} connect_rate={r.connect_rate} "
          f"qualified={r.qualified} hot={r.hot_transfers} opt_outs={r.opt_outs}")
    print(f"CRM writes recorded by MockCRM: {len(crm.write_log)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("synthetic_calls.jsonl")))
