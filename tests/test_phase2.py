"""Phase 2 checkpoint: simulator, Erlang A, SARIMA forecast, morning roster, calibrated days."""

import json
import math
import time
from datetime import date as Date

import numpy as np
import pytest

from wfm.config import load_centre, load_levers
from wfm.data.scenarios import load_scenarios
from wfm.forecast.sarima import BACKTEST_JSON, day_forecast
from wfm.schemas import (AppliedAction, CancelCoaching, ChatDeflection, IVRChange, MoveAgents,
                         OfferOvertime)
from wfm.sim.day import prepare_day, run_day
from wfm.sim.engine import PENDING
from wfm.staffing.erlang import erlang_a, required_agents
from wfm.staffing.roster import morning_plan, on_phone_coverage, requirements
from wfm.timegrid import hhmm_to_minutes


def act(t: str, action) -> AppliedAction:
    return AppliedAction(decided_at=t, action=action)


# --- Erlang A -----------------------------------------------------------------------------


def test_erlang_a_reduces_to_erlang_c_without_abandonment():
    a, n, aht = 10.0, 12, 180
    s = sum(a**k / math.factorial(k) for k in range(n))
    top = a**n / math.factorial(n) * n / (n - a)
    p_wait_c = top / (s + top)
    sl_c = 1 - p_wait_c * math.exp(-(n - a) * 20 / aht)
    r = erlang_a(a * 900 / aht, aht, 1e9, n)
    assert r.p_wait == pytest.approx(p_wait_c, abs=1e-4)
    assert r.service_level == pytest.approx(sl_c, abs=1e-4)


def test_erlang_a_behaves_sensibly():
    results = [erlang_a(100, 300, 180, n) for n in range(30, 45)]
    sls = [r.service_level for r in results]
    abandons = [r.p_abandon for r in results]
    assert sls == sorted(sls) and abandons == sorted(abandons, reverse=True)
    n = required_agents(100, 300, 180, 0.8)
    assert erlang_a(100, 300, 180, n).service_level >= 0.8 > erlang_a(100, 300, 180, n - 1).service_level


# --- forecast and roster --------------------------------------------------------------------


def test_forecast_backtest_is_accurate():
    bt = json.loads(BACKTEST_JSON.read_text())
    for q, m in bt["queues"].items():
        assert m["daily_mape_pct"] < 8, q
        assert m["interval_wape_pct"] < 15, q


def test_day_forecast_has_every_interval():
    f = day_forecast(Date(2026, 10, 6))
    assert len(f.intervals) == 48 * 2
    assert all(i.calls >= 0 and i.aht_sec > 0 for i in f.intervals)


def test_roster_follows_rules_and_covers_need():
    forecast, roster = morning_plan("2026-10-06")
    need = requirements(forecast)
    for a in roster.agents:
        start, end = hhmm_to_minutes(a.shift_start), hhmm_to_minutes(a.shift_end)
        assert hhmm_to_minutes("08:00") <= start < end <= hhmm_to_minutes("20:00")
        blocks = sorted((hhmm_to_minutes(b.start), b.duration_min) for b in [*a.breaks, *a.sessions])
        for (s1, d1), (s2, _) in zip(blocks, blocks[1:]):
            assert s1 + d1 <= s2, f"{a.id} has overlapping off-phone blocks"
        assert all(start <= s and s + d <= end for s, d in blocks)
        if end - start == 510:
            assert [b.kind for b in a.breaks] == ["break", "lunch", "break"]
    for q in need:
        assert (on_phone_coverage(roster, q) >= need[q]).mean() >= 0.9
    cross = sum(len(a.skills) == 2 for a in roster.agents) / len(roster.agents)
    assert 0.2 <= cross <= 0.3


# --- simulator --------------------------------------------------------------------------------


def test_replay_is_fast_and_deterministic():
    t = time.perf_counter()
    a = run_day("OUT-01")
    assert time.perf_counter() - t < 5  # plan checkpoint: under 2 minutes
    b = run_day("OUT-01")
    assert np.array_equal(a.out.status, b.out.status)
    assert np.allclose(np.nan_to_num(a.out.answered_at), np.nan_to_num(b.out.answered_at))


def test_every_caller_is_accounted_for():
    r = run_day("OUT-01", [act("11:00", IVRChange(queue="payments", mode="callback", start="11:00", end="13:00"))])
    assert (r.out.status != PENDING).all()
    for m in r.interval_metrics():
        assert m.offered == m.answered + m.abandoned + m.callbacks


def test_normal_day_meets_promise_and_demo_day_breaks():
    assert run_day("NORMAL-01").summary().service_level_pct >= 80
    demo = run_day("OUT-01").summary("10:20", "13:30").by_queue["payments"]
    assert demo.service_level_pct < 40 and demo.abandon_rate_pct > 15


def test_what_if_replays_the_past_identically():
    base = run_day("OUT-01")
    fixed = run_day("OUT-01", [
        act("11:00", OfferOvertime(queue="payments", start="11:00", end="13:00", agents_offered=10)),
        act("11:00", MoveAgents(from_queue="general", to_queue="payments", agents=5, start="11:00", end="13:00")),
    ])
    before = lambda r: [m for m in r.interval_metrics(as_of="11:00") if m.interval_start < "11:00"]  # noqa: E731
    assert before(base) == before(fixed)
    assert fixed.summary("11:00").by_queue["payments"].service_level_pct > \
        base.summary("11:00").by_queue["payments"].service_level_pct


def test_overtime_respects_lead_time_and_costs_money():
    r = run_day("OUT-01", [act("11:00", OfferOvertime(queue="payments", start="11:00", end="13:00",
                                                      agents_offered=20))])
    base = run_day("OUT-01")
    minute = lambda hhmm: hhmm_to_minutes(hhmm) - 480  # noqa: E731
    assert r.staffed["payments"][minute("11:15")] == base.staffed["payments"][minute("11:15")]
    assert r.staffed["payments"][minute("11:45")] > base.staffed["payments"][minute("11:45")]
    assert r.execution.costs["overtime"] > 0


def test_move_helps_receiver_and_hurts_donor():
    base = run_day("MKT-01")  # general queue campaign
    moved = run_day("MKT-01", [act("10:00", MoveAgents(from_queue="payments", to_queue="general",
                                                       agents=8, start="10:00", end="15:00"))])
    b, m = base.summary("10:00", "15:00").by_queue, moved.summary("10:00", "15:00").by_queue
    assert m["general"].service_level_pct > b["general"].service_level_pct
    assert m["payments"].service_level_pct < b["payments"].service_level_pct


def test_deflection_only_touches_matching_reasons():
    eligible = set(load_levers()["chat_deflection"]["limits"]["eligible_reasons"])
    r = run_day("OUT-01", [
        act("11:00", IVRChange(queue="payments", mode="outage_message", start="11:00", end="13:00",
                               related_reasons=["upi_payment_failed"])),
        act("11:00", ChatDeflection(queue="payments", start="11:00", end="13:00", share=0.2,
                                    reasons=["payment_status_enquiry", "card_declined"])),
    ])
    inp = r.inputs
    ivr = inp.reason[r.out.status == 3]
    chat = inp.reason[r.out.status == 4]
    assert len(ivr) > 0 and set(ivr) == {"upi_payment_failed"}
    assert len(chat) > 0 and set(chat) <= eligible  # card_declined cannot go to chat


def test_mandatory_training_is_never_cancelled():
    r = run_day("OUT-01", [act("08:00", CancelCoaching(window_start="08:00", window_end="20:00"))])
    roster = prepare_day("OUT-01").roster
    for a in roster.agents:
        for s in a.sessions:
            if s.mandatory:
                plan = next(p for p in r.execution.agents if p.id == a.id)
                t = (hhmm_to_minutes(s.start) - 480) * 60 + 1
                assert not any(w0 <= t < w1 for w0, w1 in plan.windows)


def test_snapshot_shows_only_what_was_knowable():
    r = run_day("OUT-01")
    snap = r.snapshot("10:30")
    assert max(m.interval_start for m in snap.actuals) == "10:15"
    assert all(i.opened_at <= "10:30" for i in snap.incidents)
    outage = next(i for i in r.scenario.incidents if i.severity == "P1")
    assert outage.id not in {i.id for i in snap.incidents}  # opened 10:31
    later = r.snapshot("12:00")
    seen = next(i for i in later.incidents if i.id == outage.id)
    assert seen.status == "open" and seen.resolved_at is None  # resolution not yet known
    assert all(e.logged_at <= "12:00" for e in later.attendance)


def test_sick_wave_shows_up_in_attendance_log():
    sc = next(s for s in load_scenarios() if s.id == "SCK-01")
    inp = prepare_day("SCK-01")
    in_scope = [a for a in inp.roster.agents if a.primary_queue in sc.shocks[0].queues]
    wave = [e for e in inp.attendance if e.note == sc.shocks[0].feed_note]
    assert abs(len(wave) / len(in_scope) - sc.shocks[0].magnitude) < 0.03


def test_every_bad_day_hurts_without_action():
    target = load_centre().service_level.target_pct
    for sc in load_scenarios():
        s = run_day(sc.id).summary(sc.shocks[0].start)
        hit = [s.by_queue[q].service_level_pct for q in sc.shocks[0].queues]
        assert min(hit) < target, sc.id


def test_calls_running_far_past_close_do_not_break_reporting():
    from wfm.sim.engine import EngineOutput
    r = run_day("NORMAL-01")
    r.out = EngineOutput(r.out.status, r.out.answered_at, r.out.ended_at, r.out.callback_served_at,
                         r.out.busy + [("payments", 43_000.0, 50_000.0)])  # a call ending ~22:00
    r.__dict__.pop("_busy_by_interval", None)
    assert len(r.interval_metrics()) == 96


def test_overtime_spend_never_exceeds_the_daily_budget():
    budget = load_levers()["offer_overtime"]["limits"]["daily_budget_inr"]
    offers = [act("10:45", OfferOvertime(queue="payments", start=f"{h}:15", end=f"{h + 2}:15", agents_offered=60))
              for h in (11, 13, 15)]
    r = run_day("OUT-01", offers)
    assert r.execution.costs["overtime"] <= budget
    assert any("budget fully booked" in n for n in r.execution.notes)
