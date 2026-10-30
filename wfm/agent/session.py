"""A whole day with the agent and a supervisor: steps 8 (approve) and 9 (execute and monitor).

Every 15 minutes: detect. When a disruption starts, the agent diagnoses, plans and
recommends; the supervisor approves, edits or rejects with a reason (which becomes a hard
constraint and triggers a re-plan). Approved actions are applied to the *real* day. The
agent then monitors and re-plans if the disruption outlasts the plan, the plan is falling
short, or new evidence appears.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol

from wfm.agent.detect import detect
from wfm.agent.graph import build_graph
from wfm.agent.llm import LLM, LLMUnavailable
from wfm.agent.options import describe_action
from wfm.agent.policy_check import check_plan
from wfm.agent.recommend import describe_constraints, parse_constraints
from wfm.config import ROOT
from wfm.schemas import (AppliedAction, ApprovalDecision, Constraints, DecisionLogEntry, Diagnosis, Plan,
                         Recommendation, Snapshot)
from wfm.sim.day import run_day
from wfm.timegrid import hhmm_to_minutes, interval_labels

TICKS = interval_labels()[1:] + ["20:00"]
REVIEW_GAP_MIN = 45  # wait at least this long after a decision before re-planning
OFF_TRACK_SL = 70.0  # recent service level below this on the disrupted queue counts as off track
MAX_REJECTIONS = 2
MAX_PLANS_PER_DAY = 5
RUNS_DIR = ROOT / "runs"


class Supervisor(Protocol):
    def review(self, rec: Recommendation, attempt: int) -> ApprovalDecision: ...


class AutoApprove:
    """Approves every recommendation (used for evaluation runs)."""

    def review(self, rec: Recommendation, attempt: int) -> ApprovalDecision:
        return ApprovalDecision(at=rec.at, decision="approve")


class ScriptedSupervisor:
    """Replays a fixed list of responses in order, then approves (used for the demo script)."""

    def __init__(self, responses: list[tuple[str, str | None]]):
        self.responses = list(responses)

    def review(self, rec: Recommendation, attempt: int) -> ApprovalDecision:
        if self.responses:
            decision, text = self.responses.pop(0)
            return ApprovalDecision(at=rec.at, decision=decision, constraint_text=text)
        return ApprovalDecision(at=rec.at, decision="approve")


@dataclass
class Episode:
    started: str
    diagnosis: Diagnosis
    queues: set[str]
    seen_feed_ids: set[str]
    absences_seen: int
    last_decision: str
    quiet_ticks: int = 0
    action_ends: list[str] = field(default_factory=list)


@dataclass
class Pending:
    """A recommendation waiting for a person to approve, edit or reject it."""

    snap: Snapshot
    state: dict
    rec: Recommendation
    plan: Plan
    attempt: int


@dataclass
class DaySession:
    scenario_id: str
    llm: LLM
    supervisor: Supervisor | None = field(default_factory=AutoApprove)  # None: a person answers via decide()
    on_event: Callable[[DecisionLogEntry], None] | None = None  # called as each step finishes (live trace)
    applied: list[AppliedAction] = field(default_factory=list)
    constraints: Constraints = field(default_factory=Constraints)
    log: list[DecisionLogEntry] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    decisions: list[ApprovalDecision] = field(default_factory=list)
    first_alert: str | None = None
    plans_made: int = 0
    agent_seconds: list[float] = field(default_factory=list)  # alert-to-recommendation time per planning round
    llm_errors: list[str] = field(default_factory=list)  # Claude outages: the day carries on without the agent
    pending: Pending | None = None

    def __post_init__(self):
        self.result = run_day(self.scenario_id)
        self.graph = build_graph(self.llm)
        self.episode: Episode | None = None

    # --- logging -----------------------------------------------------------------------------

    def _emit(self, entry: DecisionLogEntry) -> None:
        self.log.append(entry)
        if self.on_event:
            self.on_event(entry)

    def _note(self, now: str, step: str, summary: str, **outputs) -> None:
        self._emit(DecisionLogEntry(scenario_id=self.scenario_id, at=now, step=step, summary=summary,
                                    outputs=outputs))

    # --- the day -------------------------------------------------------------------------------

    def run(self) -> "DaySession":
        for tick in TICKS:
            self.step(tick)
        return self

    def step(self, now: str) -> None:
        if self.llm_errors or self.pending:  # assistant down, or a person still has to decide
            return
        snap = self.result.snapshot(now)
        alerts = detect(snap)
        ep = self.episode
        if ep is None:
            if alerts:
                self.first_alert = self.first_alert or now
                self._plan(snap, mode="full")
            return
        if not alerts:
            ep.quiet_ticks += 1
            if ep.quiet_ticks >= 2:
                self._note(now, "monitor", "Back on plan for 30 minutes; episode closed.")
                self.episode = None
            return
        ep.quiet_ticks = 0
        if hhmm_to_minutes(now) - hhmm_to_minutes(ep.last_decision) < REVIEW_GAP_MIN:
            return
        feed_ids = {i.id for i in snap.incidents} | {c.id for c in snap.calendar}
        new_evidence = bool(feed_ids - ep.seen_feed_ids) or len(snap.attendance) >= ep.absences_seen + 5
        recent = self._recent_sl(snap, ep.queues)
        off_track = recent is not None and recent < OFF_TRACK_SL
        expired = bool(ep.action_ends) and max(ep.action_ends) <= now
        if not (new_evidence or off_track or expired):
            self._note(now, "monitor", f"On track (recent service level {recent:.0f}%)." if recent is not None
                       else "On track.")
            return
        if self.plans_made >= MAX_PLANS_PER_DAY:
            self._note(now, "escalate", "Daily re-planning limit reached; duty manager to review.")
            ep.last_decision = now
            return
        why = ", ".join(w for w, flag in [("new evidence", new_evidence), (f"recent SL {recent:.0f}%" if recent
                                          is not None else "", off_track), ("plan has ended", expired)] if flag)
        self._note(now, "monitor", f"Re-assessing: {why}.")
        self._plan(snap, mode="full" if new_evidence or ep.diagnosis.label.startswith("unknown") else "replan")

    @staticmethod
    def _recent_sl(snap: Snapshot, queues: set[str]) -> float | None:
        rows = sorted((m for m in snap.actuals if m.queue in queues), key=lambda m: m.interval_start)[-2 * len(queues):]
        denom = sum(m.offered - m.callbacks for m in rows)
        return 100 * sum(m.answered_in_threshold for m in rows) / denom if denom else None

    # --- plan, approve, execute -----------------------------------------------------------------

    def _plan(self, snap: Snapshot, mode: str) -> None:
        state = {"scenario_id": self.scenario_id, "mode": mode, "snapshot": snap, "applied": list(self.applied),
                 "constraints": self.constraints, "log": []}
        if mode == "replan":
            state["diagnosis"] = self.episode.diagnosis
            state["alerts"] = detect(snap)
        self._run(snap, state, attempt=0)

    def _invoke(self, state: dict) -> dict:
        """Run the agent graph, emitting each step's log entries the moment that step finishes."""
        out = dict(state)
        for chunk in self.graph.stream(state, stream_mode="updates"):
            for update in chunk.values():
                for key, value in (update or {}).items():
                    if key == "log":
                        for entry in value:
                            self._emit(entry)
                    else:
                        out[key] = value
        return out

    def _run(self, snap: Snapshot, state: dict, attempt: int) -> None:
        t0 = time.perf_counter()
        try:
            out = self._invoke(state)
        except LLMUnavailable as exc:
            self.llm_errors.append(f"{snap.now}: {exc}")
            self._note(snap.now, "escalate", f"{exc}; duty manager to plan manually. No changes applied.")
            return
        self.agent_seconds.append(round(time.perf_counter() - t0, 1))
        self.plans_made += 1
        if "recommendation" not in out:  # detector found nothing after all
            return
        rec: Recommendation = out["recommendation"]
        self.recommendations.append(rec)
        self._open_episode(snap, out)
        plan = next((e.plan for e in rec.options if e.plan.id == rec.recommended_plan_id), None)
        if plan is None or not plan.actions:
            self._note(snap.now, "approval", "No change recommended" + (" (escalated)" if rec.escalate else "")
                       + "; nothing to approve.")
            return
        pending = Pending(snap, {**state, "diagnosis": out["diagnosis"], "alerts": out.get("alerts", [])},
                          rec, plan, attempt)
        if self.supervisor is None:
            self.pending = pending
            return
        self._resolve(pending, self.supervisor.review(rec, attempt))

    def decide(self, decision: ApprovalDecision) -> None:
        """A person's answer to the pending recommendation."""
        if self.pending is None:
            raise RuntimeError("no recommendation is waiting for a decision")
        pending, self.pending = self.pending, None
        self._resolve(pending, decision)

    def _resolve(self, p: Pending, decision: ApprovalDecision) -> None:
        now = p.snap.now
        self.decisions.append(decision)
        self._note(now, "approval", f"Supervisor: {decision.decision}"
                   + (f' - "{decision.constraint_text}"' if decision.constraint_text else ""), plan_id=p.plan.id)
        if decision.decision == "approve":
            self._execute(now, p.plan)
            return
        if decision.decision == "edit" and decision.edited_plan is not None:
            problems = check_plan(decision.edited_plan, snap=p.snap, applied=self.applied,
                                  constraints=self.constraints, diagnosis=p.rec.diagnosis)
            if problems:
                self._note(now, "escalate", "Edited plan not applied: " + "; ".join(
                    f"{v.rule_id} {v.message}" for v in problems))
                self.pending = p
                return
            self._execute(now, decision.edited_plan)
            return
        if p.attempt >= MAX_REJECTIONS:
            self._note(now, "escalate", f"Recommendation rejected {MAX_REJECTIONS + 1} times; duty manager to decide.")
            return
        if decision.constraint_text:
            try:
                self.constraints = parse_constraints(decision.constraint_text, self.constraints, self.llm)
            except LLMUnavailable as exc:
                self.llm_errors.append(f"{now}: {exc}")
                self._note(now, "escalate", f"{exc}; duty manager to plan manually. No changes applied.")
                return
            self._note(now, "approval", "New limits for today: " + describe_constraints(self.constraints) + ".",
                       constraints=self.constraints.model_dump(exclude_defaults=True))
        state = {**p.state, "mode": "replan", "constraints": self.constraints, "log": [], "evaluated": [],
                 "plans": [], "refined": False}
        self._run(p.snap, state, p.attempt + 1)

    def _open_episode(self, snap: Snapshot, out: dict) -> None:
        diagnosis = out["diagnosis"]
        queues = {a.queue for a in out.get("alerts", [])} or {"payments", "general"}
        feed_ids = {i.id for i in snap.incidents} | {c.id for c in snap.calendar}
        if self.episode is None:
            self.episode = Episode(snap.now, diagnosis, queues, feed_ids, len(snap.attendance), snap.now)
        else:
            ep = self.episode
            ep.diagnosis, ep.queues = diagnosis, ep.queues | queues
            ep.seen_feed_ids, ep.absences_seen, ep.last_decision = feed_ids, len(snap.attendance), snap.now

    def _execute(self, now: str, plan: Plan) -> None:
        new = [AppliedAction(decided_at=now, action=a) for a in plan.actions]
        self.applied += new
        self.result = run_day(self.scenario_id, self.applied)
        ends = [getattr(a, "end", None) or getattr(a, "window_end") for a in plan.actions]
        self.episode.action_ends = ends
        self.episode.last_decision = now
        notes = [n for n in self.result.execution.notes][-len(new):]
        self._note(now, "execute", f"Applied {plan.id} {plan.name}: " + "; ".join(describe_action(a) for a in plan.actions),
                   executor=notes)

    # --- results -----------------------------------------------------------------------------------

    def outcome(self, since: str | None = None):
        return self.result.summary(since or self.first_alert or "08:00")

    def save_log(self) -> str:
        folder = RUNS_DIR / self.scenario_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"day-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        path.write_text("".join(json.dumps(e.model_dump()) + "\n" for e in self.log))
        return str(path.relative_to(ROOT))

