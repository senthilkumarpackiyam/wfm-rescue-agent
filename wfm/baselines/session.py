"""Shared day loop for the comparison approaches (rule-based playbook, plain LLM).

Same detector and same ticks as the agent, so every approach notices the disruption at the
same moment. Plans are applied as soon as they are made (auto-approved) and are *not*
simulated or policy-checked first; that is exactly what the agent adds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from wfm.agent.detect import detect
from wfm.schemas import Alert, AppliedAction, Diagnosis, Plan, Snapshot
from wfm.sim.day import run_day
from wfm.timegrid import hhmm_to_minutes, interval_labels

TICKS = interval_labels()[1:] + ["20:00"]
REVIEW_GAP_MIN = 60
MAX_ROUNDS = 3


@dataclass
class PlanRecord:
    at: str
    plan: Plan
    diagnosis: Diagnosis  # what the approach believed (used for the CX-01 check)
    explanation: str = ""
    seconds: float = 0.0


@dataclass
class BaselineSession:
    scenario_id: str
    applied: list[AppliedAction] = field(default_factory=list)
    plans: list[PlanRecord] = field(default_factory=list)
    first_alert: str | None = None
    problems: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.result = run_day(self.scenario_id)
        self._last: str | None = None
        self._quiet = 0
        self._ends: list[str] = []

    def propose(self, snap: Snapshot, alerts: list[Alert], round_no: int) -> PlanRecord | None:
        raise NotImplementedError

    def run(self) -> "BaselineSession":
        for tick in TICKS:
            snap = self.result.snapshot(tick)
            alerts = detect(snap)
            if not alerts:
                self._quiet += 1
                if self._quiet >= 2:
                    self._last = None
                continue
            self._quiet = 0
            self.first_alert = self.first_alert or tick
            if self._last is not None:
                waited = hhmm_to_minutes(tick) - hhmm_to_minutes(self._last)
                ended = not self._ends or max(self._ends) <= tick
                if waited < REVIEW_GAP_MIN or not ended or len(self.plans) >= MAX_ROUNDS:
                    continue
            t0 = time.perf_counter()
            rec = self.propose(snap, alerts, len(self.plans))
            self._last = tick
            if rec is None or not rec.plan.actions:
                continue
            rec.seconds = rec.seconds or round(time.perf_counter() - t0, 1)
            self.plans.append(rec)
            self.applied += [AppliedAction(decided_at=tick, action=a) for a in rec.plan.actions]
            self._ends = [getattr(a, "end", None) or getattr(a, "window_end") for a in rec.plan.actions]
            self.result = run_day(self.scenario_id, self.applied)
        return self

    def outcome(self, since: str):
        return self.result.summary(since)
