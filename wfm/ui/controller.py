"""Demo controller: one disruption, two copies of the same day (no agent / agent), one clock.

Pure Python so the whole demo flow is testable without a browser; app/dashboard.py only draws it.
"""

from __future__ import annotations

import json
from typing import Callable

from wfm.agent.llm import LLM
from wfm.agent.session import DaySession
from wfm.data.scenarios import get_scenario, load_scenarios
from wfm.schemas import ApprovalDecision, DecisionLogEntry, IntervalMetrics, Plan
from wfm.sim.day import DayResult, run_day
from wfm.timegrid import clock_to_sim, day_length_minutes, sim_to_clock

def scenario_choices() -> dict[str, str]:
    """Scenario id -> label for the 'inject a disruption' picker (demo day first)."""
    out = {}
    for s in sorted(load_scenarios(include_control=True), key=lambda s: (s.id != "OUT-01", s.id)):
        out[s.id] = f"{s.id} · {s.title}"
    return out


class DemoController:
    def __init__(self, scenario_id: str, llm_mode: str = "record", start: str = "08:00",
                 on_event: Callable[[DecisionLogEntry], None] | None = None):
        self.scenario = get_scenario(scenario_id)
        self.llm = LLM(mode=llm_mode)
        self.baseline = run_day(scenario_id)
        self.events: list[DecisionLogEntry] = []
        self.on_event = on_event
        self.session = DaySession(scenario_id, self.llm, supervisor=None, on_event=self._record)
        self.minute = 0  # sim minutes since open
        self._next_tick = 15
        self.advance_to(clock_to_sim(start))

    # --- clock -----------------------------------------------------------------------------------

    def _record(self, entry: DecisionLogEntry) -> None:
        self.events.append(entry)
        if self.on_event:
            self.on_event(entry)

    @property
    def clock(self) -> str:
        return sim_to_clock(self.minute)

    @property
    def waiting(self) -> bool:
        return self.session.pending is not None

    @property
    def finished(self) -> bool:
        return self.minute >= day_length_minutes() and not self.waiting

    def advance_to(self, minute: float) -> None:
        """Move the clock forward, letting the agent act at every 15-minute tick on the way.
        Stops early (at that tick) when a recommendation needs the supervisor."""
        if self.waiting:
            return
        target = min(minute, day_length_minutes())
        while self._next_tick <= target:
            self.minute = self._next_tick
            self.session.step(sim_to_clock(self._next_tick))
            self._next_tick += 15
            if self.waiting:
                return
        self.minute = max(self.minute, target)

    def advance(self, minutes: float) -> None:
        self.advance_to(self.minute + minutes)

    @staticmethod
    def _notable(e: DecisionLogEntry) -> bool:
        return not (e.step == "monitor" and e.summary.startswith("On track"))

    def next_event(self) -> None:
        """Run until the agent does something worth watching, needs a decision, or the day ends."""
        seen = sum(self._notable(e) for e in self.events)
        while not self.finished and not self.waiting and sum(self._notable(e) for e in self.events) == seen:
            self.advance_to(self._next_tick)

    def run_to_end(self) -> None:
        self.advance_to(day_length_minutes())

    # --- the human moment --------------------------------------------------------------------------

    @property
    def pending_plan(self) -> Plan | None:
        return self.session.pending.plan if self.waiting else None

    def approve(self, keep: list[int] | None = None) -> None:
        """Approve the plan; `keep` = indices of actions to keep (approve with edits)."""
        plan = self.pending_plan
        at = self.session.pending.rec.at
        if keep is None or len(keep) == len(plan.actions):
            self.session.decide(ApprovalDecision(at=at, decision="approve"))
        else:
            edited = plan.model_copy(update={"id": plan.id + "-edited", "name": plan.name + " (edited)",
                                             "actions": [a for i, a in enumerate(plan.actions) if i in keep]})
            self.session.decide(ApprovalDecision(at=at, decision="edit", edited_plan=edited))

    def reject(self, reason: str) -> None:
        self.session.decide(ApprovalDecision(at=self.session.pending.rec.at, decision="reject",
                                             constraint_text=reason.strip() or None))

    # --- what the screen shows ---------------------------------------------------------------------

    def result(self, side: str) -> DayResult:
        return self.baseline if side == "baseline" else self.session.result

    def metrics(self, side: str) -> list[IntervalMetrics]:
        """Completed 15-minute intervals up to the clock."""
        done = {sim_to_clock(m) for m in range(0, int(self.minute) - 14, 15)}
        return [m for m in self.result(side).interval_metrics(as_of=self.clock) if m.interval_start in done]

    def so_far(self, side: str, since: str = "08:00", queues: tuple[str, ...] = ("payments", "general")) -> dict:
        rows = [m for m in self.result(side).interval_metrics(as_of=self.clock)
                if m.queue in queues and since <= m.interval_start < self.clock]
        denom = sum(m.offered - m.callbacks for m in rows)
        return {"service_level_pct": round(100 * sum(m.answered_in_threshold for m in rows) / denom, 1) if denom else None,
                "abandoned": sum(m.abandoned for m in rows),
                "calls": sum(m.offered + m.deflected for m in rows)}

    def action_cost(self, side: str) -> float:
        return 0.0 if side == "baseline" else self.session.result.action_cost

    def executed(self) -> list[DecisionLogEntry]:
        return [e for e in self.events if e.step == "execute"]

    def flagged_injections(self) -> list[str]:
        flags = []
        for e in self.events:
            if e.step == "diagnose":
                flags += e.outputs.get("diagnosis", {}).get("flagged_injections", [])
        return sorted(set(flags))

    def audit_jsonl(self) -> str:
        return "".join(json.dumps(e.model_dump()) + "\n" for e in self.session.log)
