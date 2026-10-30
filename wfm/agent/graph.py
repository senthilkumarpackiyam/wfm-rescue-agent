r"""The agent as a LangGraph state machine: the six boxes of the architecture diagram.

    full:    detect --(no alerts)--> END
               \--> diagnose --> reforecast --> generate_options --> simulate --> check_policy --> recommend --> END
    replan:  reforecast --> generate_options --> ...           (after a rejection or a monitor trigger)
    check_policy --(nothing compliant reaches target, first round)--> generate_options (refine) --> simulate ...

Approval, execution and monitoring sit outside the agent (wfm.agent.session): the supervisor
decides, and only then does anything touch the simulated contact centre.
Every node appends DecisionLogEntry records, the audit trail the supervisor can replay.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from wfm.agent.detect import detect
from wfm.agent.diagnose import DiagnosisRun, describe_alerts, diagnose
from wfm.agent.llm import LLM
from wfm.agent.options import OptionsRun, describe_action, generate_options, refine_options
from wfm.agent.recommend import (TARGET_SL, Evaluated, baseline_projection, check_policies, extra_cost, recommend,
                                 results_table, simulate_plans)
from wfm.agent.reforecast import reforecast
from wfm.schemas import (Alert, AppliedAction, Constraints, DecisionLogEntry, Diagnosis, Plan, Recommendation,
                         Reforecast, Snapshot)


class AgentState(TypedDict, total=False):
    scenario_id: str
    mode: str  # "full" or "replan"
    snapshot: Snapshot
    alerts: list[Alert]
    diagnosis_run: DiagnosisRun
    diagnosis: Diagnosis
    reforecast: Reforecast
    projected: object  # DayInputs of the agent's projected day
    baseline: object  # DayResult of that day with no further action
    applied: list[AppliedAction]
    constraints: Constraints
    options_run: OptionsRun
    plans: list[Plan]
    evaluated: list[Evaluated]
    refined: bool
    recommendation: Recommendation
    log: Annotated[list[DecisionLogEntry], operator.add]


def build_graph(llm: LLM, plan: bool = True):
    """plan=False stops after the re-forecast (the Phase 3 assess-only loop)."""

    def _entry(state, step, summary, inputs=None, outputs=None) -> DecisionLogEntry:
        snap = state["snapshot"]
        return DecisionLogEntry(scenario_id=snap.scenario_id, at=snap.now, step=step, summary=summary,
                                inputs=inputs or {}, outputs=outputs or {})

    def detect_node(state: AgentState) -> dict:
        alerts = detect(state["snapshot"])
        summary = describe_alerts(alerts) if alerts else "No gap beyond thresholds."
        return {"alerts": alerts, "log": [_entry(state, "detect", summary,
                                                 outputs={"alerts": [a.model_dump() for a in alerts]})]}

    def diagnose_node(state: AgentState) -> dict:
        run = diagnose(state["snapshot"], state["alerts"], llm)
        d = run.diagnosis
        return {"diagnosis_run": run, "diagnosis": d, "log": [_entry(
            state, "diagnose", f"{d.label} ({d.confidence}): {d.explanation}",
            inputs={"tool_calls": run.tool_calls},
            outputs={"diagnosis": d.model_dump(), "corrections": run.corrections, "seconds": run.seconds})]}

    def reforecast_node(state: AgentState) -> dict:
        rf = reforecast(state["snapshot"], state["diagnosis"], state.get("alerts", []))
        short = sorted((g for g in rf.gaps if g.gap > 0), key=lambda g: -g.gap)[:3]
        summary = " ".join(rf.assumptions) + (" Largest gaps: " + ", ".join(
            f"{g.queue} {g.interval_start} short {g.gap:g}" for g in short) if short else "")
        out = {"reforecast": rf, "log": [_entry(state, "reforecast", summary,
                                                outputs={"assumptions": rf.assumptions,
                                                         "volume_factor": rf.volume_factor,
                                                         "aht_factor": rf.aht_factor})]}
        if plan:
            snap = state["snapshot"]
            out["projected"], out["baseline"] = baseline_projection(snap.scenario_id, snap, rf,
                                                                    state.get("applied", []))
        return out

    def options_node(state: AgentState) -> dict:
        snap = state["snapshot"]
        if state.get("evaluated") and not state.get("refined"):  # second round after a failed first round
            run = state["options_run"]
            new = refine_options(run, results_table([e.evaluation for e in state["evaluated"]]), snap, llm)
            plans, refined = state["plans"] + new, True
        else:
            run = generate_options(snap, state["reforecast"], state["diagnosis"], state.get("applied", []),
                                   state.get("constraints", Constraints()), llm, baseline=state.get("baseline"))
            plans, refined, new = run.plans, False, run.plans
        summary = "; ".join(f"{p.id} {p.name}" for p in new) or "no usable plans"
        return {"options_run": run, "plans": plans, "refined": refined, "log": [_entry(
            state, "generate_options", ("Refined: " if refined else "") + summary,
            inputs={"policy_lookups": run.tool_calls},
            outputs={"plans": [{"id": p.id, "name": p.name, "rationale": p.rationale,
                                "actions": [describe_action(a) for a in p.actions]} for p in new],
                     "problems": run.problems})]}

    def simulate_node(state: AgentState) -> dict:
        snap = state["snapshot"]
        ev = simulate_plans(snap.scenario_id, snap, state["reforecast"], state.get("applied", []), state["plans"],
                            projected=state.get("projected"))
        evals = [e.evaluation for e in ev]
        summary = "; ".join(f"{e.plan.id} {e.predicted.service_level_pct:.0f}% (+₹{extra_cost(e, evals):,.0f})"
                            for e in evals)
        window = f"{evals[0].predicted.window_start}-{evals[0].predicted.window_end}"
        return {"evaluated": ev, "log": [_entry(state, "simulate", f"Predicted service level {window}: " + summary)]}

    def policy_node(state: AgentState) -> dict:
        ev = check_policies(state["evaluated"], state["snapshot"], state.get("applied", []),
                            state.get("constraints", Constraints()), state["diagnosis"])
        bad = [f"{e.evaluation.plan.id} {v.rule_id}" for e in ev for v in e.evaluation.violations]
        return {"evaluated": ev, "log": [_entry(state, "check_policy",
                                                "Violations: " + ", ".join(bad) if bad else "All plans follow policy.",
                                                outputs={"table": results_table([e.evaluation for e in ev])})]}

    def recommend_node(state: AgentState) -> dict:
        rec = recommend(state["snapshot"], state["diagnosis"], state["evaluated"],
                        state.get("constraints", Constraints()), llm)
        step = "escalate" if rec.escalate else "recommend"
        return {"recommendation": rec, "log": [_entry(state, step, rec.explanation,
                                                      outputs={"recommended_plan_id": rec.recommended_plan_id,
                                                               "escalation_reason": rec.escalation_reason})]}

    def after_policy(state: AgentState) -> str:
        good = [e for e in state["evaluated"] if not e.evaluation.violations
                and e.evaluation.predicted.service_level_pct >= TARGET_SL]
        return "recommend" if good or state.get("refined") or len(state["plans"]) == 0 else "generate_options"

    g = StateGraph(AgentState)
    for name, fn in [("detect", detect_node), ("diagnose", diagnose_node), ("reforecast", reforecast_node),
                     ("generate_options", options_node), ("simulate", simulate_node),
                     ("check_policy", policy_node), ("recommend", recommend_node)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, lambda s: "reforecast" if s.get("mode") == "replan" else "detect")
    g.add_conditional_edges("detect", lambda s: "diagnose" if s["alerts"] else END)
    g.add_edge("diagnose", "reforecast")
    g.add_edge("reforecast", "generate_options" if plan else END)
    g.add_edge("generate_options", "simulate")
    g.add_edge("simulate", "check_policy")
    g.add_conditional_edges("check_policy", after_policy)
    g.add_edge("recommend", END)
    return g.compile()
