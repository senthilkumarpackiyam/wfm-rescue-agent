"""Step 1, Detect: deterministic gap checks on the latest completed intervals.

Plain code on purpose: it runs every 15 minutes and must never hallucinate. A gap only
counts when it is both large (percent) and unlikely to be noise (z-score): quiet periods
have big percentage swings by chance, so a fixed percentage rule alone cries wolf.
Thresholds are calibrated on 31 calm days (scripts/calibrate_detector.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from wfm.config import load_centre
from wfm.schemas import Alert, IntervalMetrics, Snapshot
from wfm.staffing.roster import on_phone_coverage
from wfm.timegrid import clock_to_sim


@dataclass(frozen=True)
class DetectorConfig:
    windows: tuple[int, ...] = (2, 6)  # 30-min view for spikes, 90-min view for slow surges
    z_threshold: float = 4.0
    volume_min_gap_pct: float = 15.0
    volume_level_cv: float = 0.06  # day-level forecast error that is not a disruption
    aht_min_gap_pct: float = 25.0
    aht_level_cv: float = 0.05
    sl_window: int = 3
    sl_floor_pct: float = 50.0  # supporting only: a dip alone is a symptom, not a disruption
    staffing_gap_pct: float = 15.0  # ordinary sick calls run ~4%; waves are 14-22% on top
    staffing_min_agents: float = 5.0  # evenings run ~25 agents, where 4 ordinary absences already read as 16%
    absence_ratio: float = 2.5  # attendance log: absences vs the ordinary daily rate...
    absence_min_extra: int = 6  # ...and at least this many beyond it


def _recent(snap: Snapshot, queue: str, window: int) -> list[IntervalMetrics]:
    rows = sorted((m for m in snap.actuals if m.queue == queue), key=lambda m: m.interval_start)
    return rows[-window:] if len(rows) >= window else []


def _volume(rows, f_rows, cfg) -> tuple[float, float, float, float]:
    arrivals = sum(m.offered + m.deflected for m in rows)
    expected = sum(f.calls for f in f_rows)
    sd = math.sqrt(expected + (cfg.volume_level_cv * expected) ** 2)
    return arrivals, expected, 100 * (arrivals - expected) / expected, (arrivals - expected) / sd


def _aht(rows, f_rows, cv, cfg) -> tuple[float, float, float, float] | None:
    answered = sum(m.answered for m in rows)
    if answered < 20:
        return None
    aht = sum(m.aht_sec * m.answered for m in rows) / answered
    f_aht = sum(f.aht_sec * f.calls for f in f_rows) / sum(f.calls for f in f_rows)
    sd = f_aht * math.sqrt(cv**2 / answered + cfg.aht_level_cv**2)
    return aht, f_aht, 100 * (aht - f_aht) / f_aht, (aht - f_aht) / sd


def detect(snap: Snapshot, cfg: DetectorConfig = DetectorConfig()) -> list[Alert]:
    centre = load_centre()
    fc = {(f.queue, f.interval_start): f for f in snap.forecast.intervals}
    alerts: dict[tuple[str, str], Alert] = {}

    def raise_alert(q, metric, actual, expected, gap):
        key = (q, metric)
        if key not in alerts or abs(gap) > abs(alerts[key].gap_pct):
            alerts[key] = Alert(at=snap.now, queue=q, metric=metric, actual=round(actual, 1),
                                expected=round(expected, 1), gap_pct=round(gap, 1))

    for q in centre.queue_ids:
        for w in cfg.windows:
            rows = _recent(snap, q, w)
            if not rows:
                continue
            f_rows = [fc[(q, m.interval_start)] for m in rows]
            actual, expected, gap, z = _volume(rows, f_rows, cfg)
            if gap >= cfg.volume_min_gap_pct and z >= cfg.z_threshold:
                raise_alert(q, "volume", actual, expected, gap)
            res = _aht(rows, f_rows, centre.queues[q].aht_cv, cfg)
            if res and res[2] >= cfg.aht_min_gap_pct and res[3] >= cfg.z_threshold:
                raise_alert(q, "aht", res[0], res[1], res[2])

        rows = _recent(snap, q, cfg.sl_window)
        denom = sum(m.offered - m.callbacks for m in rows)
        if rows and denom > 0:
            sl = 100 * sum(m.answered_in_threshold for m in rows) / denom
            if sl < cfg.sl_floor_pct:
                target = centre.service_level.target_pct
                raise_alert(q, "service_level", sl, target, 100 * (sl - target) / target)

        rows = _recent(snap, q, 2)
        if rows:
            idx = [clock_to_sim(m.interval_start) // 15 for m in rows]
            plan = float(np.mean(on_phone_coverage(snap.roster, q)[idx]))
            actual = float(np.mean([m.agents_staffed for m in rows]))
            if (plan > 0 and plan - actual >= cfg.staffing_min_agents
                    and 100 * (plan - actual) / plan >= cfg.staffing_gap_pct):
                raise_alert(q, "staffing", actual, plan, 100 * (actual - plan) / plan)
        # Attendance log: a wave of sick calls is visible before the phones feel it.
        team = [a.id for a in snap.roster.agents if a.primary_queue == q]
        absent = len({e.agent_id for e in snap.attendance if e.agent_id in set(team)})
        usual = len(team) * centre.staffing.normal_absence_pct / 100
        if absent >= cfg.absence_ratio * usual and absent - usual >= cfg.absence_min_extra:
            raise_alert(q, "staffing", len(team) - absent, len(team) - usual, -100 * (absent - usual) / len(team))

    if all(a.metric == "service_level" for a in alerts.values()):
        return []
    return list(alerts.values())
