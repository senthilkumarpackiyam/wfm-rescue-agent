"""Step 5, Check policy: hard limits from config/levers.yaml and the supervisor's constraints,
enforced in code. Rule IDs match data/policy/*.md so violations cite the written rule.

Some rules need the what-if result (e.g. MOV-03: the donor queue must stay >= 70%), so the
check takes the simulated outcome of the plan as well as the plan itself.
"""

from __future__ import annotations

import math

from wfm.agent.evidence import Evidence
from wfm.config import load_centre, load_levers
from wfm.schemas import (AppliedAction, ChatDeflection, Constraints, DelayBreaks, Diagnosis,
                         IVRChange, MoveAgents, OfferOvertime, Plan, PolicyViolation, Snapshot)
from wfm.staffing.roster import on_phone_coverage
from wfm.timegrid import hhmm_to_minutes, minutes_to_hhmm

ELEVATED_RATIO = 1.3  # a reason counts as "rising" for an IVR message at 1.3x its normal level


def _m(hhmm: str) -> int:
    return hhmm_to_minutes(hhmm)


def overtime_expected_cost(action: OfferOvertime, decided_at: str) -> float:
    cfg = load_levers()["offer_overtime"]
    start = max(_m(action.start), _m(decided_at) + cfg["delay_minutes"])
    hours = max(0, _m(action.end) - start) / 60
    return action.agents_offered * cfg["limits"]["acceptance_rate"] * hours * cfg["cost_inr_per_hour"]


def _moved_breaks(snap: Snapshot, actions: list[tuple[str, DelayBreaks]]) -> dict[str, tuple[int, int, int, int]]:
    """Agent -> (original start, new start, duration, index of the action that moved it).
    Mirrors the executor: the first matching break only, and never a second break per agent."""
    moved: dict[str, tuple[int, int, int, int]] = {}
    for i, (decided, act) in enumerate(actions):
        for a in snap.roster.agents:
            if (act.queue and a.primary_queue != act.queue) or a.id in moved:
                continue
            for b in sorted(a.breaks, key=lambda b: b.start):
                s = _m(b.start)
                if _m(act.window_start) <= s < _m(act.window_end) and s >= _m(decided):
                    moved[a.id] = (s, min(s + act.delay_minutes, _m(a.shift_end) - b.duration_min), b.duration_min, i)
                    break
    return moved


def _longest_stretch(agent, move: tuple[int, int, int, int]) -> int:
    """Longest run of minutes worked without a break or lunch, after the move."""
    shift = {(_m(b.start), b.duration_min) for b in agent.breaks}
    orig, new, dur, _ = move
    shift.discard((orig, dur))
    shift.add((new, dur))
    t, longest = _m(agent.shift_start), 0
    for s, d in sorted(shift):
        longest = max(longest, s - t)
        t = s + d
    return max(longest, _m(agent.shift_end) - t)


def check_plan(plan: Plan, *, snap: Snapshot, applied: list[AppliedAction], constraints: Constraints,
               diagnosis: Diagnosis, predicted=None) -> list[PolicyViolation]:
    """`predicted` is the plan's simulated DayResult (needed for MOV-03 and queue floors)."""
    lv = load_levers()
    centre = load_centre()
    now = snap.now
    out: list[PolicyViolation] = []

    def v(rule, lever, msg):
        out.append(PolicyViolation(rule_id=rule, lever=lever, message=msg))

    for act in plan.actions:
        if act.kind in constraints.forbidden_levers:
            v("SUP-01", act.kind, f"the supervisor has ruled out {act.kind.replace('_', ' ')} today")
        start = getattr(act, "start", None) or getattr(act, "window_start", None)
        end = getattr(act, "end", None) or getattr(act, "window_end", None)
        if start and end and _m(end) <= max(_m(start), _m(now)):
            v("ACT-01", act.kind, f"window {start}-{end} has already ended or is empty at {now}")

    # Breaks: BRK-01 size of delay, BRK-02 continuous work. BRK-04 (one delay per agent per day) is
    # enforced by the executor, which skips agents whose break was already delayed.
    delays = [(a.decided_at, a.action) for a in applied if isinstance(a.action, DelayBreaks)]
    new_delays = [(now, a) for a in plan.actions if isinstance(a, DelayBreaks)]
    cap = lv["delay_breaks"]["limits"]["max_delay_minutes"]
    if constraints.max_break_delay_minutes is not None:
        cap = min(cap, constraints.max_break_delay_minutes)
    for _, act in new_delays:
        if act.delay_minutes > cap:
            v("BRK-01", "delay_breaks", f"delay of {act.delay_minutes} min exceeds the {cap} min limit")
    if new_delays:
        limit = lv["delay_breaks"]["limits"]["max_continuous_work_hours"] * 60
        agents = {a.id: a for a in snap.roster.agents}
        moved = _moved_breaks(snap, delays + new_delays)
        too_long = [aid for aid, mv in moved.items() if mv[3] >= len(delays)
                    and _longest_stretch(agents[aid], mv) > limit]
        if too_long:
            v("BRK-02", "delay_breaks", f"{len(too_long)} agents would work more than {limit // 60} h without a "
              f"break (e.g. {', '.join(too_long[:3])})")

    # Overtime: OT-02 hours per offer, OT-04 budget (or the supervisor's lower cap).
    ot_cfg = lv["offer_overtime"]
    budget = ot_cfg["limits"]["daily_budget_inr"]
    if constraints.overtime_budget_inr is not None:
        budget = min(budget, constraints.overtime_budget_inr)
    committed = sum(overtime_expected_cost(a.action, a.decided_at) for a in applied
                    if isinstance(a.action, OfferOvertime))
    planned = 0.0
    for act in plan.actions:
        if isinstance(act, OfferOvertime):
            eff_start = max(_m(act.start), _m(now) + ot_cfg["delay_minutes"])
            if _m(act.end) - eff_start > ot_cfg["limits"]["max_overtime_hours_per_agent"] * 60:
                v("OT-02", "offer_overtime", f"offer {minutes_to_hhmm(eff_start)}-{act.end} asks for more than "
                  f"{ot_cfg['limits']['max_overtime_hours_per_agent']} h per agent")
            planned += overtime_expected_cost(act, now)
    if planned and committed + planned > budget:
        v("OT-04", "offer_overtime", f"expected overtime Rs {committed + planned:,.0f} (incl. Rs {committed:,.0f} "
          f"already committed) exceeds today's budget of Rs {budget:,.0f}")

    # Moves: MOV-01 cross-skilled only, MOV-02 at most 30% of donor queue, MOV-03 donor stays >= 70%.
    absent = {e.agent_id for e in snap.attendance}
    for act in plan.actions:
        if not isinstance(act, MoveAgents):
            continue
        if act.from_queue == act.to_queue:
            v("MOV-01", "move_cross_skilled", "donor and receiving queue are the same")
            continue
        s_idx = (max(_m(act.start), _m(now)) - _m(centre.open_time)) // 15
        cross = [a for a in snap.roster.agents if a.primary_queue == act.from_queue and act.to_queue in a.skills
                 and a.id not in absent and _m(a.shift_start) <= _m(centre.open_time) + 15 * s_idx < _m(a.shift_end)]
        if act.agents > len(cross):
            v("MOV-01", "move_cross_skilled", f"only {len(cross)} cross-skilled {act.from_queue} agents are on shift "
              f"at {minutes_to_hhmm(_m(centre.open_time) + 15 * s_idx)}; {act.agents} requested")
        on_shift = float(on_phone_coverage(snap.roster, act.from_queue)[min(s_idx, 47)])
        cap = math.floor(lv["move_cross_skilled"]["limits"]["max_share_of_donor_queue"] * on_shift)
        if constraints.max_agents_moved is not None:
            cap = min(cap, constraints.max_agents_moved)
        if act.agents > cap:
            v("MOV-02", "move_cross_skilled", f"{act.agents} agents is more than the {cap} allowed from "
              f"{act.from_queue} (30% of {on_shift:.0f} on shift)")
        if predicted is not None:
            w0 = minutes_to_hhmm(max(_m(act.start), _m(now)))
            if _m(act.end) > _m(w0):
                donor = predicted.summary(w0, act.end).by_queue[act.from_queue]
                floor = lv["move_cross_skilled"]["limits"]["donor_min_service_level_pct"]
                if donor.service_level_pct < floor:
                    v("MOV-03", "move_cross_skilled", f"{act.from_queue} predicted at {donor.service_level_pct:.0f}% "
                      f"during the move ({w0}-{act.end}), below the {floor}% floor")

    # Customer channels: CX-01 IVR message only for a real, rising issue; CX-03 chat limits; CX-04 fraud.
    eligible = set(lv["chat_deflection"]["limits"]["eligible_reasons"])
    for act in plan.actions:
        if isinstance(act, IVRChange) and act.mode == "outage_message":
            if "fraud_report" in act.related_reasons:
                v("CX-04", "ivr_callback", "fraud reports may never be deflected by an IVR message")
            confirmed = diagnosis.label == "outage" or any(e.source == "incident" for e in diagnosis.evidence)
            rising = {r["reason"] for r in Evidence(snap).reason_codes(act.queue, 4)["reasons"]
                      if (r["ratio_to_normal"] or 0) >= ELEVATED_RATIO}
            if not confirmed:
                v("CX-01", "ivr_callback", "a known-issue message needs a confirmed outage or incident")
            if not act.related_reasons or not set(act.related_reasons) <= rising:
                v("CX-01", "ivr_callback", f"message must target only rising reasons on {act.queue} "
                  f"({', '.join(sorted(rising)) or 'none rising'})")
        if isinstance(act, ChatDeflection):
            if act.share > lv["chat_deflection"]["limits"]["max_deflection_share"]:
                v("CX-03", "chat_deflection", f"chat share {act.share:.0%} exceeds the 20% limit")
            bad = set(act.reasons) - eligible
            if "fraud_report" in act.reasons:
                v("CX-04", "chat_deflection", "fraud reports may never be moved to chat")
            elif bad:
                v("CX-03", "chat_deflection", f"not eligible for chat: {', '.join(sorted(bad))}")

    # Supervisor floors on a queue's service level.
    if predicted is not None:
        for f in constraints.queue_floors:
            sl = predicted.summary(now).by_queue[f.queue].service_level_pct
            if sl < f.min_service_level_pct:
                v("SUP-02", "plan", f"{f.queue} predicted at {sl:.0f}%, below the supervisor's "
                  f"{f.min_service_level_pct:.0f}% floor")
    return out

