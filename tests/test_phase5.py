"""Phase 5 checkpoint: the demo runs end to end on the outage day without errors.

Uses saved Claude answers (offline mode); skips if they have not been recorded yet."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from wfm.ui.controller import DemoController, scenario_choices

APP = str(Path(__file__).resolve().parent.parent / "app" / "dashboard.py")


def _controller(start="09:45"):
    c = DemoController("OUT-01", llm_mode="cached", start=start)
    return c


def _skip_if_unrecorded(c: DemoController):
    if c.session.llm_errors:
        pytest.skip("saved Claude answers missing for this flow: run scripts/phase4_run.py once")


def test_picker_lists_every_day_with_the_demo_first():
    choices = scenario_choices()
    assert len(choices) == 31 and next(iter(choices)) == "OUT-01"


def test_clock_stops_for_the_supervisor_and_resumes():
    c = _controller()
    assert c.clock == "09:45" and not c.waiting
    c.next_event()
    _skip_if_unrecorded(c)
    assert c.waiting and c.clock == "10:45"
    c.advance(60)
    assert c.clock == "10:45"  # nothing moves while a person has to decide
    before = c.so_far("agent", "10:20")
    assert before == c.so_far("baseline", "10:20")  # identical until the agent acts


def test_reject_replan_approve_and_finish():
    c = _controller()
    c.next_event()
    _skip_if_unrecorded(c)
    c.reject("no overtime budget today")
    _skip_if_unrecorded(c)
    assert c.waiting and "offer_overtime" in c.session.constraints.forbidden_levers
    assert any(e.summary.startswith("New limits for today: no overtime") for e in c.events)
    for _ in range(8):
        if c.finished:
            break
        if c.waiting:
            c.approve()
        else:
            c.run_to_end()
    _skip_if_unrecorded(c)
    assert c.finished
    since = c.session.first_alert
    assert c.session.outcome().service_level_pct > c.baseline.summary(since).service_level_pct + 10
    assert not [a for a in c.session.applied if a.action.kind == "offer_overtime"]
    assert c.audit_jsonl().count("\n") == len(c.session.log)


def test_approve_with_edits_drops_the_unticked_action():
    c = _controller()
    c.next_event()
    _skip_if_unrecorded(c)
    plan = c.pending_plan
    c.approve(keep=list(range(1, len(plan.actions))))  # untick the first action
    applied = [a.action for a in c.session.applied]
    assert plan.actions[0] not in applied and applied == plan.actions[1:]


def test_dashboard_click_through_runs_without_errors():
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    assert not at.exception
    at.sidebar.radio(key="mode").set_value("Offline: saved answers only")
    at.sidebar.slider(key="pause").set_value(0.0)
    at.sidebar.button(key="load").click().run()
    at.button(key="next_event").click().run()
    ctrl = at.session_state.ctrl
    _skip_if_unrecorded(ctrl)
    assert ctrl.waiting and any("Recommendation waiting" in m.value for m in at.markdown)
    at.text_input(key="reason").input("no overtime budget today")
    at.button(key="reject").click().run()
    for _ in range(10):
        ctrl = at.session_state.ctrl
        if ctrl.finished:
            break
        at.button(key="approve" if ctrl.waiting else "to_end").click().run()
    _skip_if_unrecorded(at.session_state.ctrl)
    assert not at.exception
    assert at.session_state.ctrl.finished
    assert any("Day complete" in m.value for m in at.markdown)
