"""Steps 5-7: simulate each plan in the agent's projected day, check policy, choose, explain.

The choice is made by code (cheapest compliant plan that reaches the target; otherwise the
compliant plan with the best service level, escalating below the floor), so it can be
audited. Claude only writes the explanation and turns a supervisor's plain-English
rejection into structured constraints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from wfm.agent.llm import LLM
from wfm.agent.options import describe_action
from wfm.agent.policy_check import check_plan
from wfm.config import load_centre
from wfm.schemas import (AppliedAction, Constraints, Diagnosis, Plan, PlanEvaluation, Recommendation, Reforecast,
                         Snapshot)
from wfm.sim.day import DayResult, prepare_day, run_day
from wfm.sim.projection import project_inputs
from wfm.timegrid import hhmm_to_minutes, minutes_to_hhmm

TARGET_SL = 80.0
ESCALATE_BELOW = 70.0  # ESC-02
MIN_WINDOW_MIN = 120  # judge plans on at least the next two hours
GAP_AGENTS = 2.0  # an interval counts as "in the disruption" when short by this many agents
EFFORT = "medium"
NO_ACTION = Plan(id="P0", name="No action", actions=[], rationale="Keep the morning plan unchanged.")


@dataclass
class Evaluated:
    evaluation: PlanEvaluation
    result: DayResult  # the plan's simulated (projected) day


def decision_window(rf: Reforecast, now: str) -> str:
    """End of the window plans are judged on: when the projected staffing gap closes, at least
    two hours ahead, at most close. A whole-day average would let a calm afternoon hide a
    crisis that is happening now."""
    close = hhmm_to_minutes(load_centre().close_time)
    short = [hhmm_to_minutes(g.interval_start) + 15 for g in rf.gaps if g.gap >= GAP_AGENTS]
    end = max([hhmm_to_minutes(now) + MIN_WINDOW_MIN, *short])
    return minutes_to_hhmm(min(end, close))


def baseline_projection(scenario_id: str, snap: Snapshot, rf: Reforecast, applied: list[AppliedAction]):
    """The agent's projected day (real past + its own re-forecast) and that day with no new action."""
    projected = project_inputs(prepare_day(scenario_id), snap, rf, applied)
    return projected, run_day(projected, applied)


def simulate_plans(scenario_id: str, snap: Snapshot, rf: Reforecast, applied: list[AppliedAction],
                   plans: list[Plan], projected=None) -> list[Evaluated]:
    """Step 5: run every plan (plus 'no action') through the same projected day from `now`."""
    if projected is None:
        projected = project_inputs(prepare_day(scenario_id), snap, rf, applied)
    end = decision_window(rf, snap.now)
    out = []
    for plan in [NO_ACTION, *plans]:
        actions = applied + [AppliedAction(decided_at=snap.now, action=a) for a in plan.actions]
        result = run_day(projected, actions)
        out.append(Evaluated(PlanEvaluation(plan=plan, predicted=result.summary(snap.now, end),
                                            rest_of_day=result.summary(snap.now)), result))
    return out


def check_policies(evaluated: list[Evaluated], snap: Snapshot, applied: list[AppliedAction],
                   constraints: Constraints, diagnosis: Diagnosis) -> list[Evaluated]:
    """Step 6: attach policy violations, using each plan's simulated day where a rule needs it."""
    for e in evaluated:
        if e.evaluation.plan.actions:
            e.evaluation.violations = check_plan(e.evaluation.plan, snap=snap, applied=applied,
                                                 constraints=constraints, diagnosis=diagnosis, predicted=e.result)
    return evaluated


def evaluate_plans(scenario_id: str, snap: Snapshot, rf: Reforecast, diagnosis: Diagnosis,
                   applied: list[AppliedAction], plans: list[Plan], constraints: Constraints) -> list[Evaluated]:
    return check_policies(simulate_plans(scenario_id, snap, rf, applied, plans), snap, applied, constraints, diagnosis)


def extra_cost(e: PlanEvaluation, evals: list[PlanEvaluation]) -> float:
    """Cost on top of what is already committed (the 'no action' option carries earlier actions)."""
    base = next((x.predicted.action_cost_inr for x in evals if x.plan.id == "P0"), 0.0)
    return round(e.predicted.action_cost_inr - base, 2)


def choose(evals: list[PlanEvaluation]) -> tuple[PlanEvaluation | None, bool, str | None]:
    ok = [e for e in evals if not e.violations]
    if not ok:
        return None, True, "No candidate plan follows policy."
    meets = [e for e in ok if e.predicted.service_level_pct >= TARGET_SL]
    if meets:
        best = min(meets, key=lambda e: (extra_cost(e, evals), -e.predicted.service_level_pct))
        return best, False, None
    best = max(ok, key=lambda e: (e.predicted.service_level_pct, -extra_cost(e, evals)))
    if best.predicted.service_level_pct < ESCALATE_BELOW:
        return best, True, (f"The best compliant plan is predicted at {best.predicted.service_level_pct:.0f}% "
                            f"({best.predicted.window_start}-{best.predicted.window_end}), below the "
                            f"{ESCALATE_BELOW:.0f}% escalation line (ESC-02).")
    return best, False, None


def results_table(evals: list[PlanEvaluation]) -> list[dict]:
    return [{"plan_id": e.plan.id, "name": e.plan.name,
             "actions": [describe_action(a) for a in e.plan.actions] or ["none"],
             "disruption_window": f"{e.predicted.window_start}-{e.predicted.window_end}",
             "predicted_service_level_pct": e.predicted.service_level_pct,
             "by_queue_service_level_pct": {q: s.service_level_pct for q, s in e.predicted.by_queue.items()},
             "rest_of_day_service_level_pct": e.rest_of_day.service_level_pct if e.rest_of_day else None,
             "predicted_abandon_rate_pct": e.predicted.abandon_rate_pct,
             "extra_cost_inr": round(extra_cost(e, evals)),
             "policy_violations": [f"{v.rule_id}: {v.message}" for v in e.violations]} for e in evals]


EXPLAIN_SYSTEM = """You write the recommendation a contact-centre shift supervisor reads before approving an intraday change. The plan has already been chosen by code (the cheapest plan that follows policy and reaches the target over the disruption window, otherwise the best available). Service levels are predicted for the disruption window (now until the staffing gap closes); the rest-of-day figure is context. Explain it plainly and briefly, using only the numbers you are given. Mention the main trade-off against the alternatives (cost, customer impact, risk to the other queue). If escalation is flagged, say clearly that the duty manager should be involved and why."""

EXPLAIN_FORMAT = {"type": "json_schema", "schema": {
    "type": "object", "additionalProperties": False, "required": ["headline", "why", "trade_offs", "watch"],
    "properties": {"headline": {"type": "string"}, "why": {"type": "string"},
                   "trade_offs": {"type": "array", "items": {"type": "string"}}, "watch": {"type": "string"}}}}


def explain(diagnosis: Diagnosis, chosen: PlanEvaluation | None, evals: list[PlanEvaluation],
            escalate: bool, reason: str | None, constraints: Constraints, llm: LLM) -> str:
    payload = {"diagnosis": {"cause": diagnosis.label, "summary": diagnosis.explanation},
               "target_service_level_pct": TARGET_SL, "chosen_plan_id": chosen.plan.id if chosen else None,
               "escalate": escalate, "escalation_reason": reason,
               "supervisor_constraints": constraints.model_dump(exclude_defaults=True) or "none",
               "options": results_table(evals)}
    msg = llm.create(system=EXPLAIN_SYSTEM, max_tokens=8000,
                     output_config={"effort": "low", "format": EXPLAIN_FORMAT},
                     messages=[{"role": "user", "content": json.dumps(payload, indent=1)}])
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return reason or "Recommendation explanation unavailable."
    parts = [d["headline"], d["why"]] + [f"Trade-off: {t}" for t in d["trade_offs"]] + [f"Watch: {d['watch']}"]
    return "\n".join(p for p in parts if p)


def recommend(snap: Snapshot, diagnosis: Diagnosis, evaluated: list[Evaluated], constraints: Constraints,
              llm: LLM) -> Recommendation:
    evals = [e.evaluation for e in evaluated]
    chosen, escalate, reason = choose(evals)
    return Recommendation(at=snap.now, diagnosis=diagnosis, options=evals,
                          recommended_plan_id=chosen.plan.id if chosen else None,
                          explanation=explain(diagnosis, chosen, evals, escalate, reason, constraints, llm),
                          escalate=escalate, escalation_reason=reason)


CONSTRAINT_FORMAT = {"type": "json_schema", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["forbidden_levers", "overtime_budget_inr", "max_break_delay_minutes", "max_agents_moved",
                 "queue_floors"],
    "properties": {
        "forbidden_levers": {"type": "array", "items": {"type": "string", "enum": [
            "delay_breaks", "cancel_coaching", "offer_overtime", "move_cross_skilled", "ivr_callback",
            "chat_deflection"]}},
        "overtime_budget_inr": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "max_break_delay_minutes": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "max_agents_moved": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "queue_floors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["queue", "min_service_level_pct"],
            "properties": {"queue": {"type": "string", "enum": ["payments", "general"]},
                           "min_service_level_pct": {"type": "number"}}}},
    }}}

CONSTRAINT_SYSTEM = """Convert a contact-centre supervisor's reason for rejecting a plan into structured limits for the rest of today. Levers: delay_breaks, cancel_coaching, offer_overtime, move_cross_skilled (moving agents between the payments and general queues), ivr_callback (IVR message or callback offer), chat_deflection. Only record limits the supervisor actually stated or clearly implied (for example "no overtime budget" means overtime is forbidden and its budget is 0). Use null or an empty list for anything not mentioned."""


def parse_constraints(text: str, current: Constraints, llm: LLM) -> Constraints:
    msg = llm.create(system=CONSTRAINT_SYSTEM, max_tokens=4000,
                     output_config={"effort": "low", "format": CONSTRAINT_FORMAT},
                     messages=[{"role": "user", "content": f"Supervisor said: {text!r}"}])
    d = json.loads(next(b.text for b in msg.content if b.type == "text"))

    def tighter(a, b):
        return b if a is None else a if b is None else min(a, b)

    floors = {f.queue: f.min_service_level_pct for f in current.queue_floors}
    for f in d["queue_floors"]:
        floors[f["queue"]] = max(floors.get(f["queue"], 0), f["min_service_level_pct"])
    return Constraints(
        forbidden_levers=sorted(set(current.forbidden_levers) | set(d["forbidden_levers"])),
        overtime_budget_inr=tighter(current.overtime_budget_inr, d["overtime_budget_inr"]),
        max_break_delay_minutes=tighter(current.max_break_delay_minutes, d["max_break_delay_minutes"]),
        max_agents_moved=tighter(current.max_agents_moved, d["max_agents_moved"]),
        queue_floors=[{"queue": q, "min_service_level_pct": v} for q, v in floors.items()],
        notes=[*current.notes, text],
    )



LEVER_WORDS = {"delay_breaks": "break delays", "cancel_coaching": "coaching cancellations",
               "offer_overtime": "overtime", "move_cross_skilled": "moving agents between queues",
               "ivr_callback": "IVR changes", "chat_deflection": "chat deflection"}


def describe_constraints(c: Constraints) -> str:
    parts = [f"no {LEVER_WORDS[k]}" for k in c.forbidden_levers]
    if c.overtime_budget_inr is not None:
        parts.append(f"overtime budget ₹{c.overtime_budget_inr:,.0f}")
    if c.max_break_delay_minutes is not None:
        parts.append(f"breaks delayed at most {c.max_break_delay_minutes} min")
    if c.max_agents_moved is not None:
        parts.append(f"at most {c.max_agents_moved} agents moved")
    parts += [f"keep {f.queue} above {f.min_service_level_pct:.0f}%" for f in c.queue_floors]
    return ", ".join(parts) or "none"
