"""Everything random about a scenario day, drawn once from its seed.

Every caller (arrival time, reason, handle time, patience) and every absence is fixed
before the simulation starts. Any plan, from any approach, then replays the *same*
callers, so differences in outcome come only from the decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

import numpy as np

from wfm.config import load_centre
from wfm.data.demand_model import day_rng, true_day
from wfm.data.shocks import shock_multiplier
from wfm.schemas import AttendanceEvent, Roster, Scenario
from wfm.timegrid import day_length_minutes, hhmm_to_minutes, minutes_to_hhmm, open_minutes

QUEUE_CODE = {"payments": 0, "general": 1}


@dataclass(frozen=True)
class CallSpec:
    idx: int
    queue: str
    arrival: float  # seconds since open
    reason: str
    handle: float  # seconds of agent work if answered
    patience: float  # seconds the caller will wait before hanging up
    u_ivr: float  # pre-drawn uniforms so lever effects are identical across plans
    u_chat: float
    u_callback: float


def _queue_multiplier(scenario: Scenario, queue: str, kind: str, clock_min: np.ndarray) -> np.ndarray:
    m = np.ones_like(clock_min, dtype=float)
    for sh in scenario.shocks:
        if sh.kind == kind and queue in sh.queues:
            m *= shock_multiplier(sh, clock_min)
    return m


def generate_calls(scenario: Scenario) -> list[CallSpec]:
    c = load_centre()
    day = Date.fromisoformat(scenario.date)
    minutes = np.arange(day_length_minutes())
    clock = open_minutes() + minutes + 0.5
    raw = []
    for qi, queue in enumerate(c.queue_ids):
        q = c.queues[queue]
        rate, aht = true_day(day, queue, day_rng(scenario.seed, day, queue))
        rng = np.random.default_rng([scenario.seed, 100 + qi])
        vol = _queue_multiplier(scenario, queue, "volume", clock)
        slow = _queue_multiplier(scenario, queue, "aht", clock)
        counts = rng.poisson(rate[minutes // c.interval_minutes] / c.interval_minutes * vol)
        minute_of_call = np.repeat(minutes, counts)
        n = len(minute_of_call)
        arrival = minute_of_call * 60 + rng.uniform(0, 60, n)

        # Extra calls created by a volume shock carry the shock's reason mix.
        extra = rng.random(n) < (1 - 1 / vol[minute_of_call])
        base_codes, base_p = list(q.reason_mix), np.array(list(q.reason_mix.values()))
        shock_mix = next((sh.extra_reason_mix for sh in scenario.shocks
                          if sh.kind == "volume" and queue in sh.queues and sh.extra_reason_mix), None)
        reasons = np.array(base_codes, dtype=object)[rng.choice(len(base_codes), n, p=base_p)]
        if shock_mix and extra.any():
            ex_codes, ex_p = list(shock_mix), np.array(list(shock_mix.values()))
            reasons[extra] = np.array(ex_codes, dtype=object)[rng.choice(len(ex_codes), extra.sum(), p=ex_p)]

        mean_handle = aht[minute_of_call // c.interval_minutes] * slow[minute_of_call]
        sigma2 = np.log(1 + q.aht_cv**2)
        handle = rng.lognormal(np.log(mean_handle) - sigma2 / 2, np.sqrt(sigma2))
        patience = rng.exponential(q.mean_patience_seconds, n)
        u = rng.random((3, n))
        raw += [(arrival[i], queue, reasons[i], handle[i], patience[i], u[0, i], u[1, i], u[2, i])
                for i in range(n)]
    raw.sort(key=lambda r: r[0])
    return [CallSpec(i, r[1], float(r[0]), str(r[2]), float(r[3]), float(r[4]), float(r[5]),
                     float(r[6]), float(r[7])) for i, r in enumerate(raw)]


def draw_absences(scenario: Scenario, roster: Roster) -> tuple[dict[str, float], list[AttendanceEvent]]:
    """Who is missing and from when (sim seconds), plus the attendance log entries.

    Ordinary sick calls (normal_absence_pct) happen every day and the plan allows for them;
    absence shocks remove a further share of agents."""
    c = load_centre()
    rng = np.random.default_rng([scenario.seed, 200])
    open_min = open_minutes()
    absent: dict[str, float] = {}
    log: list[AttendanceEvent] = []

    def morning_call(agent_start: int) -> str:
        return minutes_to_hhmm(max(6 * 60, agent_start - int(rng.integers(30, 91))))

    for a in roster.agents:
        if rng.random() < c.staffing.normal_absence_pct / 100:
            start = hhmm_to_minutes(a.shift_start)
            absent[a.id] = (start - open_min) * 60
            log.append(AttendanceEvent(agent_id=a.id, logged_at=morning_call(start), status="sick",
                                       note="Called in sick"))

    for sh in scenario.shocks:
        if sh.kind != "absence":
            continue
        start = hhmm_to_minutes(sh.start)
        pool = [a for a in roster.agents if a.primary_queue in sh.queues and a.id not in absent
                and hhmm_to_minutes(a.shift_end) > start]
        k = int(round(sh.magnitude * len(pool)))
        for i in sorted(rng.choice(len(pool), size=k, replace=False)):
            a = pool[i]
            a_start = hhmm_to_minutes(a.shift_start)
            if start <= a_start:  # never turns up
                absent[a.id] = (a_start - open_min) * 60
                logged = morning_call(a_start) if start <= open_min else minutes_to_hhmm(a_start - 10)
            else:  # leaves part-way through the shift
                leave = start + int(rng.integers(0, 46))
                absent[a.id] = (leave - open_min) * 60
                logged = minutes_to_hhmm(leave)
            log.append(AttendanceEvent(agent_id=a.id, logged_at=logged, status=sh.feed_status or "sick",
                                       note=sh.feed_note or ""))
    log.sort(key=lambda e: e.logged_at)
    return absent, log
