"""Demo dashboard: the same bad day side by side, without (left) and with (right) the agent.

Run:  make demo        (or: uv run streamlit run app/dashboard.py)
"""

from __future__ import annotations

import html
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from wfm.agent.options import describe_action
from wfm.agent.recommend import extra_cost
from wfm.schemas import DecisionLogEntry
from wfm.ui.controller import DemoController, scenario_choices

st.set_page_config(page_title="WFM Rescue Agent", page_icon="📞", layout="wide")

# Reference palette: categorical slots 1-2 in fixed order; chrome stays neutral in both themes.
AGENT, NO_ACTION, FORECAST, TARGET = "#2a78d6", "#eb6834", "#898781", "#c3c2b7"
ICON = {"detect": "🔔", "diagnose": "🔎", "reforecast": "📈", "generate_options": "🧩", "simulate": "🧪",
        "check_policy": "📜", "recommend": "✅", "approval": "🙋", "execute": "⚙️", "monitor": "👀",
        "escalate": "🚨"}
LABEL = {"detect": "Detect", "diagnose": "Diagnose", "reforecast": "Re-forecast", "generate_options": "Options",
         "simulate": "Simulate", "check_policy": "Check policy", "recommend": "Recommend",
         "approval": "Supervisor", "execute": "Execute", "monitor": "Monitor", "escalate": "Escalate"}
MODES = {"Saved answers, live if missing": "record", "Offline: saved answers only": "cached", "Always live": "live"}

st.markdown("""
<style>
.clock {font-size: 3rem; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums;}
.status {font-size: 1.1rem; opacity: .75;}
.trace {font-size: 1.12rem; line-height: 1.45; padding: .35rem 0; border-bottom: 1px solid rgba(128,128,128,.25);}
.trace .t {font-variant-numeric: tabular-nums; opacity: .7; margin-right: .4rem;}
.trace .s {font-weight: 700; margin-right: .4rem;}
.side {font-size: 1.25rem; font-weight: 700; margin-bottom: .25rem;}
</style>
""", unsafe_allow_html=True)


# --- controls ----------------------------------------------------------------------------------------

choices = scenario_choices()
STARTS = ["08:00", "09:00", "09:45", "10:00", "11:00", "12:00", "13:00"]
qp = st.query_params  # bookmarkable demo states, e.g. ?scenario=OUT-01&start=11:00&mode=cached
with st.sidebar:
    st.header("Intraday WFM Rescue Agent")
    ids = list(choices)
    scenario = st.selectbox("Inject a disruption", ids, format_func=choices.get, key="scenario",
                            index=ids.index(qp["scenario"]) if qp.get("scenario") in ids else 0)
    start = st.selectbox("Start the clock at", STARTS, key="start",
                         index=STARTS.index(qp["start"]) if qp.get("start") in STARTS else 2)
    modes = list(MODES)
    mode_index = list(MODES.values()).index(qp["mode"]) if qp.get("mode") in MODES.values() else 0
    mode_label = st.radio("AI answers", modes, key="mode", index=mode_index)
    speed = st.select_slider("Simulated minutes per second", [1, 2, 5, 10, 15, 30], value=1, key="speed")
    pause = st.slider("Pause between agent steps (seconds; 0 when running live)", 0.0, 3.0, 1.0, 0.5, key="pause")
    focus = st.radio("Charts show", ["payments", "general", "both queues"], key="focus", horizontal=True)
    if st.button("Load disruption", type="primary", key="load", width="stretch") or "ctrl" not in st.session_state:
        st.session_state.ctrl = DemoController(scenario, llm_mode=MODES[mode_label], start=start)
        st.session_state.playing = False
        st.session_state.last_wall = time.time()
    st.caption("Plans are tested on the agent's own forecast of the day; only approved plans touch the real day.")

ctrl: DemoController = st.session_state.ctrl
ctrl.llm.mode = MODES[mode_label]


# --- drawing helpers -----------------------------------------------------------------------------------

def queues() -> tuple[str, ...]:
    return ("payments", "general") if focus == "both queues" else (focus,)


def series(side: str):
    """Per-interval service level and calls for the focus queue(s), completed intervals only."""
    rows: dict[str, dict] = {}
    for m in ctrl.metrics(side):
        if m.queue not in queues():
            continue
        r = rows.setdefault(m.interval_start, {"ok": 0, "den": 0, "calls": 0})
        r["ok"] += m.answered_in_threshold
        r["den"] += m.offered - m.callbacks
        r["calls"] += m.offered
    hours = [int(t[:2]) + int(t[3:]) / 60 + 0.125 for t in rows]
    sl = [100 * r["ok"] / r["den"] if r["den"] else None for r in rows.values()]
    return hours, sl, [r["calls"] for r in rows.values()]


def forecast_calls():
    fc: dict[str, float] = {}
    for f in ctrl.session.result.inputs.forecast.intervals:
        if f.queue in queues():
            fc[f.interval_start] = fc.get(f.interval_start, 0) + f.calls
    return [int(t[:2]) + int(t[3:]) / 60 + 0.125 for t in fc], list(fc.values())


def base_layout(fig: go.Figure, title: str, y_title: str, y_range=None) -> go.Figure:
    fig.update_layout(height=230, margin=dict(l=10, r=10, t=36, b=10), showlegend=False,
                      title=dict(text=title, font=dict(size=14)), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font=dict(color=FORECAST))
    fig.update_xaxes(range=[8, 20], tickvals=list(range(8, 21, 2)),
                     ticktext=[f"{h:02d}:00" for h in range(8, 21, 2)], showgrid=False)
    fig.update_yaxes(title=y_title, range=y_range, gridcolor="rgba(128,128,128,.2)", zeroline=False)
    clock_h = 8 + ctrl.minute / 60
    fig.add_vline(x=clock_h, line=dict(color=FORECAST, width=1, dash="dot"))
    return fig


def side_panel(side: str, color: str, title: str):
    shock = ctrl.scenario.shocks[0].start if ctrl.scenario.shocks else "08:00"
    since = shock if ctrl.clock > shock else "08:00"
    now = ctrl.so_far(side, since, queues())
    other = ctrl.so_far("baseline" if side == "agent" else "agent", since, queues())
    st.markdown(f'<div class="side" style="color:{color}">{title}</div>', unsafe_allow_html=True)
    # fixed height so the agent side's delta badges do not push its charts below the other side's
    c1, c2, c3 = st.container(height=125, border=False).columns(3)
    sl = now["service_level_pct"]
    gain = sl - other["service_level_pct"] if sl is not None and other["service_level_pct"] is not None else 0
    fewer = now["abandoned"] - other["abandoned"]
    c1.metric(f"Service level since {since}", "-" if sl is None else f"{sl:.0f}%",
              f"{gain:+.0f} pts vs no agent" if side == "agent" and abs(gain) >= 0.5 else None)
    c2.metric("Callers who hung up", f"{now['abandoned']:,}",
              f"{fewer:+,} vs no agent" if side == "agent" and fewer else None, delta_color="inverse")
    c3.metric("Cost of actions", f"₹{ctrl.action_cost(side):,.0f}")
    hours, sl_series, calls = series(side)
    fig = go.Figure()
    fig.add_hline(y=80, line=dict(color=TARGET, width=1, dash="dash"))
    fig.add_trace(go.Scatter(x=hours, y=sl_series, mode="lines", line=dict(color=color, width=2),
                             hovertemplate="%{y:.0f}%<extra></extra>"))
    if side == "agent":
        for e in ctrl.executed():
            h = int(e.at[:2]) + int(e.at[3:]) / 60
            fig.add_vline(x=h, line=dict(color=AGENT, width=1))
    st.plotly_chart(base_layout(fig, "Service level every 15 min (dashed = 80% promise)", "%", [0, 105]),
                    width="stretch", key=f"sl_{side}")
    fx, fy = forecast_calls()
    fig = go.Figure()
    fig.add_trace(go.Bar(x=hours, y=calls, marker_color=color, opacity=.85, width=0.22,
                         hovertemplate="%{y} calls<extra></extra>"))
    fig.add_trace(go.Scatter(x=fx, y=fy, mode="lines", line=dict(color=FORECAST, width=2),
                             hovertemplate="forecast %{y:.0f}<extra></extra>"))
    st.plotly_chart(base_layout(fig, "Calls reaching agents vs morning forecast (grey line)", "calls"),
                    width="stretch", key=f"calls_{side}")


def short(text: str, limit: int = 260) -> str:
    """First line, cut at a sentence end near the limit (full text is in the audit log and decision card)."""
    first = text.split("\n")[0]
    if len(first) <= limit:
        return first
    cut = first.rfind(". ", 0, limit)
    return first[: cut + 1] if cut > 80 else first[:limit] + "..."


def trace_html(e: DecisionLogEntry) -> str:
    text = html.escape(short(e.summary))
    return (f'<div class="trace"><span class="t">{e.at}</span>{ICON.get(e.step, "•")} '
            f'<span class="s">{LABEL.get(e.step, e.step)}</span>{text}</div>')


def render_trace(box, show: int = 12):
    shown = [e for e in ctrl.events if not (e.step == "monitor" and e.summary.startswith("On track"))]
    with box.container(height=520):
        if not shown:
            st.caption("The agent checks the day every 15 minutes. Nothing to report yet.")
        for e in reversed(shown[-show:]):
            st.markdown(trace_html(e), unsafe_allow_html=True)


def streaming(trace_box):
    """Callback that redraws the trace as each agent step finishes, pausing so the audience can read it."""
    def show(entry: DecisionLogEntry):
        render_trace(trace_box)
        if entry.step not in ("monitor", "detect"):
            time.sleep(pause)
    return show


def decision_card(trace_box):
    p = ctrl.session.pending
    rec = p.rec
    card = st.empty()
    with card.container():
        st.markdown("### 🙋 Recommendation waiting for you")
        if rec.escalate:
            st.warning(f"Escalation: {rec.escalation_reason}")
        headline, _, details = rec.explanation.partition("\n")
        st.markdown(f"**{html.escape(headline)}**", unsafe_allow_html=True)
        with st.expander("Why, and the trade-offs"):
            st.markdown(html.escape(details).replace("\n", "<br><br>"), unsafe_allow_html=True)
        evals = rec.options
        window = f"{evals[0].predicted.window_start}-{evals[0].predicted.window_end}"
        table = pd.DataFrame([{
            "Plan": f"{'➡ ' if e.plan.id == rec.recommended_plan_id else ''}{e.plan.id} "
                    f"{e.plan.name if len(e.plan.name) <= 32 else e.plan.name[:31] + '…'}",
            f"SL {window}": f"{e.predicted.service_level_pct:.0f}%",
            "Rest of day": f"{e.rest_of_day.service_level_pct:.0f}%" if e.rest_of_day else "-",
            "Hang-ups": f"{e.predicted.abandon_rate_pct:.1f}%",
            "Extra cost": f"₹{extra_cost(e, evals):,.0f}",
            "Policy": "OK" if not e.violations else " ".join(sorted({v.rule_id for v in e.violations})),
        } for e in evals])
        st.dataframe(table, hide_index=True, width="stretch")
        st.markdown(f"**{p.plan.id} {p.plan.name}**: untick anything you do not want.")
        keep = [i for i, a in enumerate(p.plan.actions)
                if st.checkbox(describe_action(a), value=True, key=f"keep_{rec.at}_{p.attempt}_{i}")]
        reason = st.text_input("Reason, if you reject", placeholder="e.g. no overtime budget today", key="reason")
        a, b = st.columns(2)
        approved = a.button("✅ Approve", type="primary", key="approve", width="stretch", disabled=not keep)
        rejected = b.button("❌ Reject and re-plan", key="reject", width="stretch")
    if approved:
        card.success("Approved. Applying the plan to the contact centre...")
        ctrl.on_event = streaming(trace_box)
        ctrl.approve(keep)
        st.session_state.last_wall = time.time()
        st.rerun()
    if rejected:
        card.info(f'Re-planning under your instruction: "{reason or "rejected without a reason"}". '
                  "Watch the agent trace.")
        ctrl.on_event = streaming(trace_box)
        ctrl.reject(reason or "rejected without a reason")
        st.rerun()


# --- the live page -------------------------------------------------------------------------------------

@st.fragment(run_every=1.0)
def live():
    now_wall = time.time()
    elapsed = now_wall - st.session_state.get("last_wall", now_wall)
    st.session_state.last_wall = now_wall

    head = st.columns([1.2, 3, 1, 1, 1, 1])
    status = ("Waiting for the supervisor" if ctrl.waiting else "Day complete" if ctrl.finished
              else "Running" if st.session_state.playing else "Paused")
    head[0].markdown(f'<div class="clock">{ctrl.clock}</div><div class="status">{status}</div>',
                     unsafe_allow_html=True)
    head[1].markdown(f"**{ctrl.scenario.date}** · {html.escape(choices[ctrl.scenario.id])}")
    play = head[2].button("⏸ Pause" if st.session_state.playing else "▶ Play", key="play",
                          width="stretch", disabled=ctrl.waiting or ctrl.finished)
    step = head[3].button("⏭ Next agent step", key="next_event", width="stretch",
                          disabled=ctrl.waiting or ctrl.finished)
    plus = head[4].button("+15 min", key="plus15", width="stretch", disabled=ctrl.waiting or ctrl.finished)
    end = head[5].button("⏩ Run to end", key="to_end", width="stretch", disabled=ctrl.waiting or ctrl.finished)
    if play:
        st.session_state.playing = not st.session_state.playing

    flags = ctrl.flagged_injections()
    if flags:
        st.error(f"🛡️ Prompt-injection attempt in {', '.join(flags)}: instructions hidden in the ticket were "
                 "ignored and reported to the supervisor (policy ESC-03).")
    if ctrl.session.llm_errors:
        st.error(f"AI assistant unavailable ({ctrl.session.llm_errors[-1]}). The day continues without it; "
                 "the duty manager plans manually.")

    left, right = st.columns(2, gap="large")
    lower_left, lower_right = st.columns([2, 3] if ctrl.waiting else [3, 2], gap="large")
    with lower_left:
        st.markdown("### Agent trace")
        trace_box = st.empty()
        render_trace(trace_box)  # refill at once: the 1-second refresh would otherwise blink the trace

    ctrl.on_event = streaming(trace_box)
    if step:
        ctrl.next_event()
    elif plus:
        ctrl.advance(15)
    elif end:
        ctrl.run_to_end()
    elif st.session_state.playing and not ctrl.waiting:
        ctrl.advance(speed * min(elapsed, 3.0))
    if ctrl.waiting or ctrl.finished:
        st.session_state.playing = False
    ctrl.on_event = None
    render_trace(trace_box)

    with left:
        side_panel("baseline", NO_ACTION, "Without the agent")
    with right:
        side_panel("agent", AGENT, "With the agent")
    with lower_right:
        if ctrl.waiting:
            decision_card(trace_box)
        elif ctrl.finished:
            since = ctrl.session.first_alert or "08:00"
            a, b = ctrl.baseline.summary(since), ctrl.session.outcome()
            st.markdown("### Day complete")
            st.markdown(f"From the first alert ({since}) to close: service level **{a.service_level_pct:.0f}% → "
                        f"{b.service_level_pct:.0f}%**, callers who hung up **{a.abandon_rate_pct:.1f}% → "
                        f"{b.abandon_rate_pct:.1f}%**, cost of actions **₹{b.action_cost_inr:,.0f}**.")
        else:
            st.markdown("### Supervisor")
            st.caption("When the agent recommends a plan, it appears here for approval.")
        u, r = ctrl.llm.usage, ctrl.llm.replayed
        st.caption(f"Claude this session: {u.calls} live calls (US${u.cost_usd:.2f}), {r.calls} replayed from saved answers."
                   + (f" Agent time to recommendation: {ctrl.session.agent_seconds[0]:.0f} s." if ctrl.session.agent_seconds
                      and u.calls else ""))
        st.download_button("Download audit log (JSONL)", ctrl.audit_jsonl(), file_name=f"{ctrl.scenario.id}-audit.jsonl",
                           key="audit", width="stretch")


live()
