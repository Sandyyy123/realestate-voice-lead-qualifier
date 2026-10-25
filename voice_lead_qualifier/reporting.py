"""Campaign reporting: dials, connections, qualified leads, transfers."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import LeadStatus

CONNECTED = {LeadStatus.HOT, LeadStatus.WARM_FOLLOW_UP, LeadStatus.NURTURE,
             LeadStatus.NOT_INTERESTED, LeadStatus.DO_NOT_CALL}


@dataclass
class Report:
    dials: int
    connected: int
    connect_rate: float
    qualified: int          # hot + warm follow-up
    hot_transfers: int
    opt_outs: int
    by_status: dict[str, int]


def build_report(outcomes) -> Report:
    counts = Counter(o.status for o in outcomes if not o.duplicate)
    dials = sum(counts.values())
    connected = sum(n for s, n in counts.items() if s in CONNECTED)
    return Report(
        dials=dials,
        connected=connected,
        connect_rate=round(connected / dials, 3) if dials else 0.0,
        qualified=counts[LeadStatus.HOT] + counts[LeadStatus.WARM_FOLLOW_UP],
        hot_transfers=counts[LeadStatus.HOT],
        opt_outs=counts[LeadStatus.DO_NOT_CALL],
        by_status={s.value: counts[s] for s in LeadStatus if counts[s]},
    )
