"""Run a scenario day and read the results.

    result = run_day("OUT-01")                                   # no action
    result = run_day("OUT-01", [AppliedAction(decided_at="10:45", action=...)])
    result.summary("10:45")          # outcome from 10:45 to close
    result.snapshot("10:45")         # what the agent is allowed to see at 10:45

A what-if from time t is simply a re-run with actions decided at t: callers are fixed by
the seed and nothing changes before t, so the past replays identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from functools import cached_property, lru_cache
from typing import Sequence

import numpy as np

from wfm.config import load_centre
from wfm.data.scenarios import get_scenario
from wfm.schemas import (AppliedAction, AttendanceEvent, DayForecast, Incident, IntervalMetrics,
                         QueueSummary, ReasonCodeCount, Roster, Scenario, SimSummary, Snapshot)
from wfm.sim.calls import QUEUE_CODE, CallSpec, draw_absences, generate_calls
from wfm.sim.engine import ABANDONED, ANSWERED, CALLBACK, Engine, EngineOutput
from wfm.sim.levers import DEFLECTED_CHAT, DEFLECTED_IVR, Execution, build_execution, staffed_by_minute
from wfm.staffing.roster import morning_plan
from wfm.timegrid import clock_to_sim, interval_labels, n_intervals, sim_to_clock

INTERVAL_SEC = 900


@dataclass(frozen=True)
class DayInputs:
    scenario: Scenario
    forecast: DayForecast
    roster: Roster
    calls: list[CallSpec]
    absent: dict[str, float]
    attendance: list[AttendanceEvent]
    ot_draws: np.ndarray
    arrival: np.ndarray = field(repr=False)
    queue_code: np.ndarray = field(repr=False)
    reason: np.ndarray = field(repr=False)
    handle: np.ndarray = field(repr=False)


@lru_cache(maxsize=48)
def prepare_day(scenario_id: str) -> DayInputs:
    """Draw everything random about the day once; every run of this day reuses it."""
    return prepare_scenario(get_scenario(scenario_id))


def prepare_scenario(sc: Scenario, roster: Roster | None = None) -> DayInputs:
    """Uncached variant for ad-hoc scenarios (calibration, live 'pick a disruption')."""
    forecast, planned = morning_plan(sc.date)
    roster = roster or planned
    calls = generate_calls(sc)
    absent, attendance = draw_absences(sc, roster)
    ot_draws = np.random.default_rng([sc.seed, 300]).random(1000)
    return DayInputs(
        sc, forecast, roster, calls, absent, attendance, ot_draws,
        arrival=np.array([c.arrival for c in calls]),
        queue_code=np.array([QUEUE_CODE[c.queue] for c in calls]),
        reason=np.array([c.reason for c in calls], dtype=object),
        handle=np.array([c.handle for c in calls]),
    )


def run_day(scenario: str | DayInputs, actions: Sequence[AppliedAction] = ()) -> "DayResult":
    inp = prepare_day(scenario) if isinstance(scenario, str) else scenario
    ex = build_execution(inp.roster, inp.calls, inp.absent, inp.ot_draws, list(actions))
    staffed = staffed_by_minute(ex.agents)
    out = Engine(inp.calls, ex, staffed).run()
    return DayResult(inp, list(actions), ex, staffed, out)


@dataclass
class DayResult:
    inputs: DayInputs
    applied: list[AppliedAction]
    execution: Execution
    staffed: dict[str, np.ndarray]  # agents on-queue per minute
    out: EngineOutput

    @property
    def scenario(self) -> Scenario:
        return self.inputs.scenario

    @property
    def action_cost(self) -> float:
        return round(sum(self.execution.costs.values()), 2)

    @cached_property
    def _busy_by_interval(self) -> np.ndarray:
        busy = np.zeros((len(QUEUE_CODE), n_intervals() + 4))
        for q, s, e in self.out.busy:
            qi, t = QUEUE_CODE[q], s
            while t < e:
                i = int(t // INTERVAL_SEC)
                if i >= busy.shape[1]:  # a long call running far past close: outside the reported day
                    break
                nxt = min(e, (i + 1) * INTERVAL_SEC)
                busy[qi, i] += nxt - t
                t = nxt
        return busy

    def _masks(self, as_of: float):
        inp, out = self.inputs, self.out
        arrived = inp.arrival < as_of
        st = out.status
        deflected = arrived & np.isin(st, (DEFLECTED_IVR, DEFLECTED_CHAT))
        callback = arrived & (st == CALLBACK)
        answered = arrived & (st == ANSWERED) & (out.answered_at <= as_of)
        abandoned = arrived & (st == ABANDONED) & (out.ended_at <= as_of)
        wait = np.where(answered, out.answered_at - inp.arrival, np.nan)
        in_thr = answered & (wait <= load_centre().service_level.threshold_seconds)
        offered = arrived & ~deflected
        return offered, answered, abandoned, in_thr, deflected, callback, wait

    def interval_metrics(self, as_of: str | None = None) -> list[IntervalMetrics]:
        """Per-interval results by arrival time, counting only outcomes known by `as_of`."""
        t = clock_to_sim(as_of) * 60 if as_of else np.inf
        offered, answered, abandoned, in_thr, deflected, callback, wait = self._masks(t)
        idx = np.minimum((self.inputs.arrival // INTERVAL_SEC).astype(int), n_intervals() - 1)
        rows = []
        for q, qi in QUEUE_CODE.items():
            inq = self.inputs.queue_code == qi
            for i, label in enumerate(interval_labels()):
                m = inq & (idx == i)
                n_off, n_ans = int((m & offered).sum()), int((m & answered).sum())
                n_cb = int((m & callback).sum())
                denom = n_off - n_cb
                staffed = float(self.staffed[q][i * 15:(i + 1) * 15].mean())
                rows.append(IntervalMetrics(
                    interval_start=label, queue=q, offered=n_off, answered=n_ans,
                    abandoned=int((m & abandoned).sum()), answered_in_threshold=int((m & in_thr).sum()),
                    deflected=int((m & deflected).sum()), callbacks=n_cb,
                    service_level_pct=round(100 * (m & in_thr).sum() / denom, 1) if denom > 0 else None,
                    asa_sec=round(float(np.nanmean(wait[m & answered])), 1) if n_ans else 0.0,
                    aht_sec=round(float(self.inputs.handle[m & answered].mean()), 1) if n_ans else 0.0,
                    agents_staffed=round(staffed, 2),
                    occupancy_pct=round(100 * min(1.0, self._busy_by_interval[qi, i] / (staffed * INTERVAL_SEC)), 1)
                    if staffed > 0 else 0.0,
                ))
        return rows

    def summary(self, window_start: str = "08:00", window_end: str | None = None) -> SimSummary:
        """Outcome for callers arriving in [window_start, window_end)."""
        c = load_centre()
        window_end = window_end or c.close_time
        a, b = clock_to_sim(window_start) * 60, clock_to_sim(window_end) * 60
        offered, answered, abandoned, in_thr, _, callback, wait = self._masks(np.inf)
        win = (self.inputs.arrival >= a) & (self.inputs.arrival < b)
        by_queue, tot = {}, np.zeros(3)
        for q, qi in QUEUE_CODE.items():
            m = win & (self.inputs.queue_code == qi)
            denom = (m & offered).sum() - (m & callback).sum()
            n_ans = (m & answered).sum()
            by_queue[q] = QueueSummary(
                offered=int((m & offered).sum()), answered=int(n_ans), abandoned=int((m & abandoned).sum()),
                service_level_pct=round(100 * (m & in_thr).sum() / denom, 1) if denom else 100.0,
                abandon_rate_pct=round(100 * (m & abandoned).sum() / denom, 1) if denom else 0.0,
                asa_sec=round(float(np.nanmean(wait[m & answered])), 1) if n_ans else 0.0,
            )
            tot += [(m & in_thr).sum(), (m & abandoned).sum(), denom]
        return SimSummary(
            window_start=window_start, window_end=window_end, by_queue=by_queue,
            service_level_pct=round(100 * tot[0] / tot[2], 1) if tot[2] else 100.0,
            abandon_rate_pct=round(100 * tot[1] / tot[2], 1) if tot[2] else 0.0,
            action_cost_inr=self.action_cost,
        )

    def reason_codes(self, as_of: str | None = None) -> list[ReasonCodeCount]:
        """IVR reason counts per interval (captured before any deflection)."""
        t = clock_to_sim(as_of) * 60 if as_of else np.inf
        arrived = self.inputs.arrival < t
        idx = np.minimum((self.inputs.arrival // INTERVAL_SEC).astype(int), n_intervals() - 1)
        labels = interval_labels()
        counts: dict[tuple[int, str, str], int] = {}
        for i, qc, r in zip(idx[arrived], self.inputs.queue_code[arrived], self.inputs.reason[arrived]):
            key = (int(i), "payments" if qc == 0 else "general", r)
            counts[key] = counts.get(key, 0) + 1
        return [ReasonCodeCount(interval_start=labels[i], queue=q, reason=r, count=n)
                for (i, q, r), n in sorted(counts.items())]

    def snapshot(self, now: str) -> Snapshot:
        """What the agent may see at `now`: completed intervals and feed items already published."""
        t_min = clock_to_sim(now)
        done = {sim_to_clock(i * 15) for i in range(n_intervals()) if (i + 1) * 15 <= t_min}
        incidents = []
        for inc in self.scenario.incidents:
            if inc.opened_at > now:
                continue
            resolved = inc.resolved_at is not None and inc.resolved_at <= now
            incidents.append(inc.model_copy(update={"status": "resolved" if resolved else "open",
                                                    "resolved_at": inc.resolved_at if resolved else None}))
        return Snapshot(
            scenario_id=self.scenario.id, date=self.scenario.date, now=now,
            actuals=[m for m in self.interval_metrics(as_of=now) if m.interval_start in done],
            forecast=self.inputs.forecast, roster=self.inputs.roster,
            reason_codes=[r for r in self.reason_codes(as_of=now) if r.interval_start in done],
            incidents=incidents, calendar=self.scenario.calendar,
            attendance=[e for e in self.inputs.attendance if e.logged_at <= now],
            applied_actions=[a for a in self.applied if a.decided_at <= now],
        )


def day_of(scenario_id: str) -> Date:
    return Date.fromisoformat(get_scenario(scenario_id).date)
