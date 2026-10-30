"""Phase 1 checkpoint: agreed data formats, synthetic history, 30 scenarios with true causes."""

import json
from collections import Counter
from datetime import date as Date

import pandas as pd
import pytest
from pydantic import TypeAdapter, ValidationError

from wfm.config import load_centre, load_levers
from wfm.data.history import HISTORY_CSV, load_history
from wfm.data.scenarios import SCENARIOS_JSON, WINDOW, build_scenarios, load_scenarios
from wfm.schemas import LeverAction, Plan
from wfm.timegrid import clock_to_sim, interval_index, n_intervals, sim_to_clock

EXPECTED_COUNTS = {
    "outage_spike": 8, "marketing_campaign": 6, "weather_seasonal_surge": 4,
    "sick_call_wave": 6, "aht_spike": 4, "adversarial": 2,
}


@pytest.fixture(scope="module")
def history() -> pd.DataFrame:
    return load_history()


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(include_control=True)


# --- config and clock ------------------------------------------------------------------


def test_config_loads():
    c = load_centre()
    assert c.queue_ids == ["payments", "general"]
    assert n_intervals() == 48
    assert set(load_levers()) == {
        "delay_breaks", "cancel_coaching", "offer_overtime",
        "move_cross_skilled", "ivr_callback", "chat_deflection",
    }


def test_clock_round_trip():
    assert clock_to_sim("10:20") == 140
    assert sim_to_clock(140) == "10:20"
    assert interval_index(clock_to_sim("10:20")) == 9  # 10:15-10:30 slot
    assert interval_index(10_000) == 47


# --- history ------------------------------------------------------------------------------


def test_history_shape(history):
    c = load_centre()
    days = (Date.fromisoformat(c.history.end_date) - Date.fromisoformat(c.history.start_date)).days + 1
    assert len(history) == days * n_intervals() * len(c.queues)
    assert (history["offered"] >= 0).all()
    assert (history["aht_sec"] > 0).all()


def test_history_has_weekly_pattern(history):
    daily = history.groupby(["date", "queue"])["offered"].sum().reset_index()
    daily["dow"] = pd.to_datetime(daily["date"]).dt.day_of_week
    by_dow = daily.groupby("dow")["offered"].mean()
    assert by_dow[0] > by_dow[2] > by_dow[6]  # Monday > Wednesday > Sunday


def test_history_has_intraday_peak(history):
    curve = history.groupby("interval_start")["offered"].mean()
    assert "10:00" <= curve.idxmax() <= "12:30"


def test_history_trend_upward(history):
    daily = history.groupby("date")["offered"].sum()
    jan_25 = daily[daily.index.str.startswith("2025-0")].mean()
    jan_26 = daily[daily.index.str.startswith("2026-0")].mean()
    assert jan_26 > jan_25


# --- scenarios ----------------------------------------------------------------------------


def test_thirty_bad_days_split_as_planned(scenarios):
    bad = [s for s in scenarios if s.type != "normal"]
    assert len(bad) == 30
    assert Counter(s.type for s in bad) == EXPECTED_COUNTS


def test_scenarios_unique_and_in_window(scenarios):
    assert len({s.id for s in scenarios}) == len(scenarios)
    assert len({s.date for s in scenarios}) == len(scenarios)
    assert len({s.seed for s in scenarios}) == len(scenarios)
    for s in scenarios:
        d = Date.fromisoformat(s.date)
        assert WINDOW[0] <= d <= WINDOW[1]
        assert d.weekday() != 6, f"{s.id} falls on a Sunday"
        assert d > Date.fromisoformat(load_centre().history.end_date)


def test_cited_evidence_exists_in_feeds(scenarios):
    for s in scenarios:
        feed_ids = {i.id for i in s.incidents} | {c.id for c in s.calendar}
        for ref in s.ground_truth.evidence_refs:
            if ":" in ref or ref == "attendance_log":
                continue
            assert ref in feed_ids, f"{s.id} cites {ref} which is not in its feeds"


def test_reason_codes_are_real(scenarios):
    c = load_centre()
    for s in scenarios:
        for sh in s.shocks:
            if sh.extra_reason_mix:
                assert abs(sum(sh.extra_reason_mix.values()) - 1) < 1e-6
                for q in sh.queues:
                    assert set(sh.extra_reason_mix) <= set(c.queues[q].reason_mix), s.id
    all_codes = {r for q in c.queues.values() for r in q.reason_mix}
    assert set(load_levers()["chat_deflection"]["limits"]["eligible_reasons"]) <= all_codes


def test_weather_days_have_no_clue(scenarios):
    for s in scenarios:
        if s.type == "weather_seasonal_surge":
            assert s.ground_truth.expected_diagnosis == "unknown_demand_surge"
            assert not s.ground_truth.evidence_refs
            assert all(sh.extra_reason_mix is None for sh in s.shocks)


def test_adversarial_days_carry_injection(scenarios):
    for s in scenarios:
        if s.type == "adversarial":
            assert s.injection_text
            assert any(s.injection_text in i.description for i in s.incidents)
            assert s.ground_truth.expected_diagnosis == "outage"
        else:
            assert s.injection_text is None


def test_demo_day_is_scripted(scenarios):
    demo = next(s for s in scenarios if s.id == "OUT-01")
    assert demo.shocks[0].start == "10:20"
    assert demo.shocks[0].queues == ["payments"]


def test_generation_is_deterministic():
    on_disk = json.loads(SCENARIOS_JSON.read_text())
    assert [s.model_dump() for s in build_scenarios()] == on_disk


# --- lever / plan contract ----------------------------------------------------------------


def test_plan_round_trip_with_mixed_levers():
    plan = Plan(
        id="P1", name="Breaks + overtime",
        actions=[
            {"kind": "delay_breaks", "window_start": "10:30", "window_end": "12:00", "delay_minutes": 30},
            {"kind": "offer_overtime", "queue": "payments", "start": "11:00", "end": "13:00",
             "agents_offered": 10},
            {"kind": "ivr_callback", "queue": "payments", "mode": "outage_message",
             "start": "10:45", "end": "13:00", "related_reasons": ["upi_payment_failed"]},
        ],
    )
    again = Plan.model_validate_json(plan.model_dump_json())
    assert [a.kind for a in again.actions] == ["delay_breaks", "offer_overtime", "ivr_callback"]


def test_bad_inputs_are_rejected():
    adapter = TypeAdapter(LeverAction)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "fire_everyone"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "offer_overtime", "queue": "payments",
                                 "start": "25:00", "end": "13:00", "agents_offered": 5})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "chat_deflection", "queue": "payments", "start": "10:00",
                                 "end": "11:00", "share": 1.5, "reasons": []})


def test_history_file_exists():
    assert HISTORY_CSV.exists()


# --- shock shape --------------------------------------------------------------------------


def test_shock_shapes():
    from wfm.data.shocks import shock_multiplier
    from wfm.schemas import Shock
    from wfm.timegrid import hhmm_to_minutes as m

    spike = Shock(kind="volume", queues=["payments"], start="10:00", duration_min=60,
                  magnitude=2.0, ramp_min=20, decay_min=30)
    assert shock_multiplier(spike, m("09:59")) == 1.0
    assert shock_multiplier(spike, m("10:10")) == pytest.approx(1.5)   # half-way up the ramp
    assert shock_multiplier(spike, m("10:45")) == 2.0                  # holding
    assert shock_multiplier(spike, m("11:15")) == pytest.approx(1.5)   # half-way down
    assert shock_multiplier(spike, m("12:00")) == 1.0

    allday = Shock(kind="volume", queues=["general"], start="09:00", magnitude=1.4)
    assert shock_multiplier(allday, m("19:59")) == pytest.approx(1.4)

    absence = Shock(kind="absence", queues=["payments"], start="13:00", magnitude=0.25)
    assert shock_multiplier(absence, m("12:59")) == 1.0
    assert shock_multiplier(absence, m("13:00")) == 0.75
