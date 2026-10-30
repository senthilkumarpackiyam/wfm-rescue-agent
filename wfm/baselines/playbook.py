"""Baseline 2: the rule-based intraday playbook a typical WFM team runs. No AI, no simulator.

When the detector fires on a queue:
  1. delay that queue's breaks by 30 min for the next hour; cancel its coaching for two hours;
  2. switch on the callback offer for two hours;
  3. call the overtime list: offer 15 agents two hours, starting in 30 minutes;
  4. if the other queue is not in trouble, borrow 4 cross-skilled agents from it for two hours;
  5. if a P1/P2 incident is open, play a known-issue message for the two busiest call reasons.
Repeated (at most three times a day) if the queue is still alerting an hour later.
"""

from __future__ import annotations

from collections import Counter

from wfm.baselines.session import BaselineSession, PlanRecord
from wfm.schemas import (Alert, CancelCoaching, DelayBreaks, Diagnosis, EvidenceRef, IVRChange, MoveAgents,
                         OfferOvertime, Plan, Snapshot)
from wfm.timegrid import hhmm_to_minutes, minutes_to_hhmm

OTHER = {"payments": "general", "general": "payments"}


def _at(now: str, minutes: int) -> str:
    return minutes_to_hhmm(min(hhmm_to_minutes(now) + minutes, 20 * 60))


class PlaybookSession(BaselineSession):
    def propose(self, snap: Snapshot, alerts: list[Alert], round_no: int) -> PlanRecord | None:
        now = snap.now
        queues = sorted({a.queue for a in alerts})
        incident = next((i for i in snap.incidents if i.status == "open" and i.severity in ("P1", "P2")), None)
        actions = []
        for q in queues:
            actions += [
                DelayBreaks(queue=q, window_start=now, window_end=_at(now, 60), delay_minutes=30),
                CancelCoaching(queue=q, window_start=now, window_end=_at(now, 120)),
                IVRChange(queue=q, mode="callback", start=now, end=_at(now, 120)),
                OfferOvertime(queue=q, start=_at(now, 30), end=_at(now, 150), agents_offered=15),
            ]
            if OTHER[q] not in queues:
                actions.append(MoveAgents(from_queue=OTHER[q], to_queue=q, agents=4, start=now, end=_at(now, 120)))
            if incident:
                recent = sorted({r.interval_start for r in snap.reason_codes if r.queue == q})[-2:]
                busiest = Counter()
                for r in snap.reason_codes:
                    if r.queue == q and r.interval_start in recent:
                        busiest[r.reason] += r.count
                actions.append(IVRChange(queue=q, mode="outage_message", start=now, end=_at(now, 120),
                                         related_reasons=[k for k, _ in busiest.most_common(2)],
                                         message_text="We are aware of a service issue and are working on it."))
        actions = [a for a in actions if (getattr(a, "end", None) or a.window_end) > now]
        belief = Diagnosis(label="outage" if incident else "none", confidence="low", explanation="playbook",
                           evidence=[EvidenceRef(source="incident", ref_id=incident.id, summary="open P1/P2")]
                           if incident else [])
        return PlanRecord(at=now, plan=Plan(id=f"PB{round_no + 1}", name="Standard intraday playbook",
                                            actions=actions), diagnosis=belief,
                          explanation="Fixed rules: breaks, coaching, callback, overtime list, borrow agents.")
