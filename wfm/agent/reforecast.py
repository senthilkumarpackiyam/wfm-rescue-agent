"""Step 3, Re-forecast: the morning forecast x a shock factor, shaped by the diagnosed cause.

All numbers come from code. The diagnosis only chooses which shape applies:
an outage fades within the hour or two, a campaign lasts the afternoon, an unexplained
surge is assumed to last all day, a slowdown fades after about an hour.
"""

from __future__ import annotations

import numpy as np

from wfm.config import load_centre
from wfm.schemas import (Alert, DayForecast, Diagnosis, IntervalForecast, Reforecast, Roster, Snapshot,
                         StaffingGap)
from wfm.staffing.erlang import required_agents
from wfm.staffing.roster import on_phone_coverage
from wfm.timegrid import clock_to_sim, interval_labels

# (minutes the current level holds, minutes it then takes to fade back to the forecast)
VOLUME_SHAPE = {"outage": (45, 60), "marketing_campaign": (180, 120), "unknown_demand_surge": (None, 0)}
AHT_SHAPE = (60, 60)
RESOLVED_SHAPE = (0, 30)
SHAPE_TEXT = {"outage": "hold for 45 min, then fade over 60 min (typical outage)",
              "marketing_campaign": "hold for 3 h, then fade over 2 h (campaign response)",
              "unknown_demand_surge": "hold until close (cause unknown, so assume it persists)"}


def _shape(minutes_ahead: float, hold: int | None, fade: int) -> float:
    if hold is None or minutes_ahead <= hold:
        return 1.0
    return max(0.0, 1 - (minutes_ahead - hold) / fade) if fade else 0.0


def reforecast(snap: Snapshot, diagnosis: Diagnosis, alerts: list[Alert]) -> Reforecast:
    centre = load_centre()
    now = clock_to_sim(snap.now)
    labels = interval_labels()
    fc = {(f.queue, f.interval_start): f for f in snap.forecast.intervals}
    cited = {e.ref_id for e in diagnosis.evidence if e.source == "incident"}
    resolved = any(i.id in cited and i.status == "resolved" for i in snap.incidents)
    label = diagnosis.label
    assumptions: list[str] = []
    out: list[IntervalForecast] = []
    gaps: list[StaffingGap] = []
    vol_f, aht_f, absent_n = {}, {}, {}

    for q in centre.queue_ids:
        # Spikes: the latest interval is the best guide (the one before may predate the start).
        # Unexplained surges: a 90-minute view, so one noisy interval does not set the level.
        window = 6 if label == "unknown_demand_surge" else 1
        rows = sorted((m for m in snap.actuals if m.queue == q), key=lambda m: m.interval_start)[-window:]
        f_rows = [fc[(q, m.interval_start)] for m in rows]
        expected = sum(f.calls for f in f_rows)
        vf = sum(m.offered + m.deflected for m in rows) / expected if expected else 1.0
        answered = sum(m.answered for m in rows)
        af = (sum(m.aht_sec * m.answered for m in rows) / answered) / (
            sum(f.aht_sec * f.calls for f in f_rows) / expected) if answered and expected else 1.0
        has_vol = any(a.queue == q and a.metric == "volume" for a in alerts)
        has_aht = any(a.queue == q and a.metric == "aht" for a in alerts)
        vf = float(np.clip(vf, 0.5, 4.0)) if has_vol and label in VOLUME_SHAPE else 1.0
        af = float(np.clip(af, 0.7, 3.0)) if has_aht and label in ("system_slowness", "unknown_supply_issue") else 1.0
        vol_f[q], aht_f[q] = round(vf, 2), round(af, 2)
        v_shape = RESOLVED_SHAPE if resolved else VOLUME_SHAPE.get(label, (0, 0))
        a_shape = RESOLVED_SHAPE if resolved else AHT_SHAPE
        if vf != 1.0:
            assumptions.append(f"{q}: calls running {vf:.2f}x forecast; assumed to "
                               + ("fade over 30 min (incident resolved)" if resolved else SHAPE_TEXT[label]) + ".")
        if af != 1.0:
            assumptions.append(f"{q}: handle time running {af:.2f}x forecast; assumed to hold 60 min, then fade over 60 min.")

        team = {a.id for a in snap.roster.agents if a.primary_queue == q}
        absent = {e.agent_id for e in snap.attendance if e.agent_id in team}
        absent_n[q] = len(absent)
        lost = on_phone_coverage(Roster(date=snap.date, agents=[a for a in snap.roster.agents if a.id in absent]), q)
        available = on_phone_coverage(snap.roster, q) - lost
        if label in ("sick_wave", "unknown_supply_issue") and absent:
            assumptions.append(f"{q}: {len(absent)} agents absent today; only absences already logged are counted.")

        patience = centre.queues[q].mean_patience_seconds
        target = centre.staffing.planning_sl_target_pct / 100
        for i, t in enumerate(labels):
            start = i * 15
            if start < now - 14:  # only intervals that have not finished yet
                continue
            ahead = start + 7.5 - now
            f = fc[(q, t)]
            calls = f.calls * (1 + (vf - 1) * _shape(ahead, *v_shape))
            aht = f.aht_sec * (1 + (af - 1) * _shape(ahead, *a_shape))
            out.append(IntervalForecast(interval_start=t, queue=q, calls=round(calls, 1), aht_sec=round(aht, 1)))
            need = required_agents(calls, aht, patience, target, centre.service_level.threshold_seconds)
            gaps.append(StaffingGap(interval_start=t, queue=q, calls=round(calls, 1), aht_sec=round(aht, 1),
                                    agents_needed=need, agents_available=float(available[i]),
                                    gap=round(need - float(available[i]), 1)))
    if not assumptions:
        assumptions.append("No lasting change projected; the morning forecast stands.")
    return Reforecast(at=snap.now, basis=label,
                      forecast=DayForecast(date=snap.date, made_at=snap.now, method=f"reforecast:{label}", intervals=out),
                      volume_factor=vol_f, aht_factor=aht_f, absent_agents=absent_n,
                      assumptions=assumptions, gaps=gaps)
