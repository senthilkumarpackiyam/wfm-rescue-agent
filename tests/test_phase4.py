"""Phase 4 checkpoint: projection, policy checks, plan choice, approval and re-plan flow.

LLM-backed tests replay saved responses (cached mode) and skip if none exist."""

import numpy as np
import pytest

from wfm.agent.detect import detect
from wfm.agent.llm import LLM
from wfm.agent.options import to_action
from wfm.agent.policy_check import check_plan
from wfm.agent.recommend import choose, extra_cost
from wfm.agent.reforecast import reforecast
from wfm.agent.session import DaySession, ScriptedSupervisor
from wfm.schemas import (AppliedAction, ChatDeflection, Constraints, DelayBreaks, Diagnosis, EvidenceRef,
                         IVRChange, MoveAgents, OfferOvertime, Plan, PlanEvaluation, QueueSummary, SimSummary)
from wfm.sim.day import prepare_day, run_day
from wfm.sim.projection import project_inputs

OUTAGE = Diagnosis(label="outage", confidence="high", explanation="t",
                   evidence=[EvidenceRef(source="incident", ref_id="INC-20261006-6998", summary="x")])


@pytest.fixture(scope="module")
def demo():
    day = run_day("OUT-01")
    snap = day.snapshot("10:45")
    rf = reforecast(snap, OUTAGE, detect(snap))
    return day, snap, rf


# --- the agent's projected day ------------------------------------------------------------------


def test_projection_replays_the_past_exactly(demo):
    day, snap, rf = demo
    proj = run_day(project_inputs(prepare_day("OUT-01"), snap, rf, []))
    before = lambda r: [m for m in r.interval_metrics(as_of="10:45") if m.interval_start < "10:45"]  # noqa: E731
    assert before(proj) == before(day)


def test_projection_does_not_see_the_real_future(demo):
    day, snap, rf = demo
    real, proj = prepare_day("OUT-01"), project_inputs(prepare_day("OUT-01"), snap, rf, [])
    cut = (10 * 60 + 45 - 480) * 60
    assert np.array_equal(real.arrival[real.arrival < cut], proj.arrival[proj.arrival < cut])
    assert not np.array_equal(real.arrival[real.arrival >= cut][:50], proj.arrival[proj.arrival >= cut][:50])
    # the real outage runs until ~13:45; the agent assumed a typical one that fades by ~12:30
    window = lambda a: ((a >= (13 * 60 - 480) * 60) & (a < (14 * 60 - 480) * 60)).sum()  # noqa: E731
    assert window(proj.arrival[proj.queue_code == 0]) < 0.8 * window(real.arrival[real.queue_code == 0])


def test_projection_knows_only_logged_absences():
    day = run_day("SCK-01")
    snap = day.snapshot("08:30")
    rf = reforecast(snap, Diagnosis(label="sick_wave", confidence="high", explanation="t", evidence=[]), detect(snap))
    proj = project_inputs(prepare_day("SCK-01"), snap, rf, [])
    assert set(proj.absent) == {e.agent_id for e in snap.attendance} < set(prepare_day("SCK-01").absent)


# --- policy checks ---------------------------------------------------------------------------------


def _check(snap, *actions, constraints=Constraints(), applied=(), diagnosis=OUTAGE, predicted=None):
    plan = Plan(id="T", name="t", actions=list(actions))
    return {v.rule_id for v in check_plan(plan, snap=snap, applied=list(applied), constraints=constraints,
                                          diagnosis=diagnosis, predicted=predicted)}


def test_a_sensible_plan_passes(demo):
    _, snap, _ = demo
    assert _check(snap,
                  DelayBreaks(queue="payments", window_start="10:45", window_end="11:30", delay_minutes=30),
                  OfferOvertime(queue="payments", start="11:15", end="13:00", agents_offered=20),
                  IVRChange(queue="payments", mode="outage_message", start="10:45", end="12:30",
                            related_reasons=["upi_payment_failed"]),
                  ChatDeflection(queue="payments", start="10:45", end="12:30", share=0.2,
                                 reasons=["payment_status_enquiry"])) == set()


def test_each_rule_fires(demo):
    _, snap, _ = demo
    assert "BRK-01" in _check(snap, DelayBreaks(window_start="10:45", window_end="11:30", delay_minutes=60))
    assert "OT-02" in _check(snap, OfferOvertime(queue="payments", start="11:15", end="14:15", agents_offered=5))
    assert "OT-04" in _check(snap, OfferOvertime(queue="payments", start="11:15", end="13:15", agents_offered=100))
    assert {"MOV-01", "MOV-02"} <= _check(snap, MoveAgents(from_queue="general", to_queue="payments", agents=30,
                                                           start="10:45", end="12:00"))
    assert "CX-03" in _check(snap, ChatDeflection(queue="payments", start="10:45", end="12:00", share=0.5,
                                                  reasons=["payment_status_enquiry"]))
    assert "CX-03" in _check(snap, ChatDeflection(queue="payments", start="10:45", end="12:00", share=0.1,
                                                  reasons=["card_declined"]))
    assert "CX-04" in _check(snap, ChatDeflection(queue="payments", start="10:45", end="12:00", share=0.1,
                                                  reasons=["fraud_report"]))
    unknown = Diagnosis(label="unknown_demand_surge", confidence="low", explanation="t", evidence=[])
    assert "CX-01" in _check(snap, IVRChange(queue="payments", mode="outage_message", start="10:45", end="12:00",
                                             related_reasons=["upi_payment_failed"]), diagnosis=unknown)
    assert "CX-01" in _check(snap, IVRChange(queue="payments", mode="outage_message", start="10:45", end="12:00",
                                             related_reasons=["fraud_report"]))


def test_supervisor_constraints_and_repeat_delays_are_enforced(demo):
    _, snap, _ = demo
    no_ot = Constraints(forbidden_levers=["offer_overtime"], overtime_budget_inr=0)
    assert {"SUP-01", "OT-04"} <= _check(snap, OfferOvertime(queue="payments", start="11:15", end="12:15",
                                                             agents_offered=5), constraints=no_ot)
    # BRK-04: the executor never delays a second break for the same agent.
    first = AppliedAction(decided_at="10:45", action=DelayBreaks(queue="payments", window_start="10:45",
                                                                 window_end="12:00", delay_minutes=30))
    again = AppliedAction(decided_at="11:30", action=DelayBreaks(queue="payments", window_start="11:30",
                                                                 window_end="13:30", delay_minutes=30))
    once = run_day("OUT-01", [first]).execution.notes
    twice = run_day("OUT-01", [first, again]).execution.notes
    assert "skipped" not in once[0] and "already delayed" in twice[1]


def test_donor_queue_floor_uses_the_simulated_outcome(demo):
    _, snap, rf = demo
    big_move = MoveAgents(from_queue="general", to_queue="payments", agents=10, start="10:45", end="13:00")
    proj = run_day(project_inputs(prepare_day("OUT-01"), snap, rf, []),
                   [AppliedAction(decided_at="10:45", action=big_move)])
    assert "MOV-03" in _check(snap, big_move, predicted=proj)


# --- reading plans and choosing ---------------------------------------------------------------------


def test_flat_actions_become_typed_levers():
    base = {"queue": None, "from_queue": None, "to_queue": None, "delay_minutes": None, "agents": None,
            "agents_offered": None, "mode": None, "related_reasons": [], "share": None, "reasons": [],
            "message_text": None}
    a = to_action({**base, "kind": "move_cross_skilled", "from_queue": "general", "to_queue": "payments",
                   "agents": 4, "start": "10:00", "end": "12:00"}, now="10:45")
    assert isinstance(a, MoveAgents) and a.start == "10:45"  # a start in the past is clamped to now
    with pytest.raises(Exception):
        to_action({**base, "kind": "offer_overtime", "queue": "payments", "start": "11:00", "end": "12:00"}, "10:45")


def _ev(pid, sl, cost, violations=()):
    q = QueueSummary(offered=1, answered=1, abandoned=0, service_level_pct=sl, abandon_rate_pct=0, asa_sec=0)
    summary = SimSummary(window_start="10:45", window_end="20:00", by_queue={"payments": q}, service_level_pct=sl,
                         abandon_rate_pct=0, action_cost_inr=cost)
    from wfm.schemas import PolicyViolation
    return PlanEvaluation(plan=Plan(id=pid, name=pid, actions=[]), predicted=summary,
                          violations=[PolicyViolation(rule_id=v, lever="x", message="x") for v in violations])


def test_choice_rule():
    best, esc, _ = choose([_ev("P0", 60, 100), _ev("P1", 85, 5000), _ev("P2", 82, 900), _ev("P3", 95, 400, ["OT-04"])])
    assert best.plan.id == "P2" and not esc  # cheapest compliant plan that reaches 80%
    best, esc, _ = choose([_ev("P0", 60, 0), _ev("P1", 75, 500), _ev("P2", 72, 100)])
    assert best.plan.id == "P1" and not esc  # nothing reaches 80%: best service level
    best, esc, why = choose([_ev("P0", 50, 0), _ev("P1", 65, 500)])
    assert best.plan.id == "P1" and esc and "ESC-02" in why
    assert extra_cost(_ev("P1", 85, 5000), [_ev("P0", 60, 1200), _ev("P1", 85, 5000)]) == 3800


# --- full days with a supervisor (replayed) -------------------------------------------------------------


def _session(sid, supervisor=None):
    s = DaySession(sid, LLM(mode="cached"), supervisor or ScriptedSupervisor([])).run()
    if s.llm_errors:  # the session survives a missing answer; the test cannot
        pytest.skip("no saved LLM responses for the current prompts: run scripts/phase4_run.py once")
    return s


def test_session_survives_claude_being_unavailable(tmp_path, monkeypatch):
    import wfm.agent.llm as llm_mod
    monkeypatch.setattr(llm_mod, "CACHE_DIR", tmp_path)  # empty cache + cached mode = Claude unreachable
    s = DaySession("OUT-01", LLM(mode="cached"), ScriptedSupervisor([])).run()
    assert s.llm_errors and not s.applied
    assert s.log[-1].step == "escalate" and "duty manager" in s.log[-1].summary


def test_agent_day_recovers_service_level():
    s = _session("OUT-01")
    base = run_day("OUT-01").summary(s.first_alert)
    assert s.outcome().service_level_pct >= base.service_level_pct + 10
    assert [e.step for e in s.log][:3] == ["detect", "diagnose", "reforecast"]
    assert any(e.step == "execute" for e in s.log)


def test_rejection_becomes_a_constraint_and_the_agent_replans():
    s = _session("OUT-01", ScriptedSupervisor([("reject", "no overtime budget today")]))
    assert "offer_overtime" in s.constraints.forbidden_levers
    assert s.decisions[0].decision == "reject" and s.decisions[1].decision == "approve"
    assert not [a for a in s.applied if isinstance(a.action, OfferOvertime)]
    assert s.outcome().service_level_pct > run_day("OUT-01").summary(s.first_alert).service_level_pct


def test_plans_are_judged_on_the_disruption_window(demo):
    from wfm.agent.recommend import decision_window
    _, snap, rf = demo
    end = decision_window(rf, snap.now)
    assert "12:30" <= end <= "13:30"  # outage gap closes around midday, not at 20:00
    snap2 = run_day("WTH-01").snapshot("12:00")
    rf2 = reforecast(snap2, Diagnosis(label="unknown_demand_surge", confidence="low", explanation="t", evidence=[]),
                     detect(snap2))
    assert decision_window(rf2, "12:00") >= "19:30"  # an unexplained surge is assumed to last all day
