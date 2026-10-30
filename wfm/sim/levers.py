"""Lever executor: turn approved actions into concrete agent schedules and call routing.

The simulator applies what it is given; policy limits are checked *before* this step
(Phase 4). Only physical facts are enforced here: nothing changes before the decision,
overtime needs its lead time, mandatory training cannot be skipped, and chat can only
take reasons that chat can serve.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from wfm.config import load_centre, load_levers
from wfm.schemas import (AppliedAction, CancelCoaching, ChatDeflection, DelayBreaks, IVRChange,
                         MoveAgents, OfferOvertime, Roster)
from wfm.sim.calls import CallSpec
from wfm.timegrid import day_length_minutes, hhmm_to_minutes, open_minutes

DEFLECTED_IVR, DEFLECTED_CHAT = 3, 4


def _sec(hhmm: str) -> float:
    return (hhmm_to_minutes(hhmm) - open_minutes()) * 60.0


@dataclass
class AgentPlan:
    id: str
    windows: list[tuple[float, float]]  # on-queue periods, seconds since open
    priority: list[str]  # default routing: first queue served first
    overrides: list[tuple[float, float, list[str]]] = field(default_factory=list)

    def routing(self, t: float) -> list[str]:
        for a, b, p in self.overrides:
            if a <= t < b:
                return p
        return self.priority


@dataclass
class Execution:
    agents: list[AgentPlan]
    deflected: dict[int, int]  # call idx -> DEFLECTED_IVR / DEFLECTED_CHAT
    callback_windows: list[tuple[str, float, float]]
    costs: dict[str, float]
    notes: list[str]


def _subtract(windows: list[tuple[float, float]], a: float, b: float) -> list[tuple[float, float]]:
    out = []
    for s, e in windows:
        if b <= s or a >= e:
            out.append((s, e))
            continue
        if s < a:
            out.append((s, a))
        if b < e:
            out.append((b, e))
    return out


def build_execution(roster: Roster, calls: list[CallSpec], absent: dict[str, float],
                    ot_draws: np.ndarray, applied: list[AppliedAction]) -> Execution:
    levers = load_levers()
    costs = {"overtime": 0.0, "coaching": 0.0, "chat": 0.0}
    notes: list[str] = []
    other = {"payments": "general", "general": "payments"}

    # 1. Off-phone blocks per agent, after break moves and coaching cancellations.
    blocks: dict[str, list[list]] = {}
    for a in roster.agents:
        blocks[a.id] = [[_sec(b.start), b.duration_min * 60, "break", False, None] for b in a.breaks]
        blocks[a.id] += [[_sec(s.start), s.duration_min * 60, "session", s.mandatory, s.id] for s in a.sessions]

    delayed: set[str] = set()  # BRK-04: one delayed break per agent per day, enforced here
    for ap in applied:
        act, t_dec = ap.action, _sec(ap.decided_at)
        if isinstance(act, DelayBreaks):
            ws, we, moved, skipped = _sec(act.window_start), _sec(act.window_end), 0, 0
            for a in roster.agents:
                if act.queue and a.primary_queue != act.queue:
                    continue
                shift_end = _sec(a.shift_end)
                for blk in blocks[a.id]:
                    if blk[2] == "break" and ws <= blk[0] < we and blk[0] >= t_dec:
                        if a.id in delayed:
                            skipped += 1
                            break
                        blk[0] = min(blk[0] + act.delay_minutes * 60, shift_end - blk[1])
                        delayed.add(a.id)
                        moved += 1
                        break
            notes.append(f"delay_breaks: {moved} breaks moved by {act.delay_minutes} min"
                         + (f" ({skipped} agents skipped: break already delayed today)" if skipped else ""))
        elif isinstance(act, CancelCoaching):
            ws, we, cancelled = _sec(act.window_start), _sec(act.window_end), 0
            rate = levers["cancel_coaching"]["cost_inr_per_session_hour"]
            for a in roster.agents:
                if act.queue and a.primary_queue != act.queue:
                    continue
                keep = []
                for blk in blocks[a.id]:
                    if blk[2] == "session" and not blk[3] and ws <= blk[0] < we and blk[0] >= t_dec:
                        cancelled += 1
                        costs["coaching"] += blk[1] / 3600 * rate
                    else:
                        keep.append(blk)
                blocks[a.id] = keep
            notes.append(f"cancel_coaching: {cancelled} sessions cancelled")

    # 2. On-queue windows = shift minus off-phone blocks, cut at any absence.
    plans: list[AgentPlan] = []
    for a in roster.agents:
        win = [(_sec(a.shift_start), _sec(a.shift_end))]
        for start, dur, *_ in blocks[a.id]:
            win = _subtract(win, start, start + dur)
        if a.id in absent:
            win = _subtract(win, absent[a.id], 1e9)
        priority = [a.primary_queue] + [q for q in a.skills if q != a.primary_queue]
        plans.append(AgentPlan(a.id, win, priority))
    by_id = {p.id: p for p in plans}
    skills = {a.id: a for a in roster.agents}

    # 3. Routing changes, overtime, IVR and chat.
    deflected: dict[int, int] = {}
    callback_windows: list[tuple[str, float, float]] = []
    ot_used, ot_n = 0, 0
    for ap in applied:
        act, t_dec = ap.action, _sec(ap.decided_at)
        if isinstance(act, MoveAgents):
            start, end = max(_sec(act.start), t_dec), _sec(act.end)
            pool = [p for p in plans
                    if skills.get(p.id) and skills[p.id].primary_queue == act.from_queue
                    and act.to_queue in skills[p.id].skills
                    and any(s < end and e > start for s, e in p.windows)
                    and not any(a < end and b > start for a, b, _ in p.overrides)]
            chosen = sorted(pool, key=lambda p: p.id)[: act.agents]
            for p in chosen:
                p.overrides.append((start, end, [act.to_queue, act.from_queue]))
            notes.append(f"move_cross_skilled: {len(chosen)} of {act.agents} requested moved "
                         f"{act.from_queue}->{act.to_queue}")
        elif isinstance(act, OfferOvertime):
            cfg = levers["offer_overtime"]
            start = max(_sec(act.start), t_dec + cfg["delay_minutes"] * 60)
            end = _sec(act.end)
            if start >= end:
                notes.append("offer_overtime: window closed before agents could log in")
                continue
            draws = ot_draws[ot_used: ot_used + act.agents_offered]
            ot_used += act.agents_offered
            accepted = int((draws < cfg["limits"]["acceptance_rate"]).sum())
            # OT-04: slots are booked first come, first served; once the day's budget is fully
            # booked, further volunteers are declined, so real spend can never exceed it.
            slot_cost = (end - start) / 3600 * cfg["cost_inr_per_hour"]
            room = cfg["limits"]["daily_budget_inr"] - costs["overtime"]
            affordable = max(0, int(room // slot_cost)) if slot_cost > 0 else 0
            if accepted > affordable:
                notes.append(f"offer_overtime: {accepted - affordable} volunteers declined, budget fully booked")
                accepted = affordable
            for _ in range(accepted):
                ot_n += 1
                plans.append(AgentPlan(f"OT-{ot_n:02d}", [(start, end)], [act.queue]))
            costs["overtime"] += accepted * (end - start) / 3600 * cfg["cost_inr_per_hour"]
            notes.append(f"offer_overtime: {accepted} of {act.agents_offered} accepted")
        elif isinstance(act, IVRChange):
            start, end = max(_sec(act.start), t_dec), _sec(act.end)
            if act.mode == "callback":
                callback_windows.append((act.queue, start, end))
                notes.append(f"ivr_callback: callback offer on {act.queue}")
            else:
                effect = levers["ivr_callback"]["effects"]["outage_message_deflection"]
                related = set(act.related_reasons)
                n = 0
                for c in calls:
                    if (c.queue == act.queue and start <= c.arrival < end and c.reason in related
                            and c.u_ivr < effect and c.idx not in deflected):
                        deflected[c.idx] = DEFLECTED_IVR
                        n += 1
                notes.append(f"ivr_callback: outage message deflected {n} calls")
        elif isinstance(act, ChatDeflection):
            cfg = levers["chat_deflection"]
            start, end = max(_sec(act.start), t_dec), _sec(act.end)
            ok = set(act.reasons) & set(cfg["limits"]["eligible_reasons"])
            n = 0
            for c in calls:
                if (c.queue == act.queue and start <= c.arrival < end and c.reason in ok
                        and c.u_chat < act.share and c.idx not in deflected):
                    deflected[c.idx] = DEFLECTED_CHAT
                    n += 1
            costs["chat"] += n * cfg["cost_inr_per_contact"]
            notes.append(f"chat_deflection: {n} calls moved to chat")

    return Execution(plans, deflected, callback_windows, costs, notes)


def staffed_by_minute(plans: list[AgentPlan]) -> dict[str, np.ndarray]:
    """Agents on-queue each minute, attributed to the queue they serve first."""
    n_min = day_length_minutes() + 60
    out = {q: np.zeros(n_min) for q in load_centre().queue_ids}
    for p in plans:
        top = np.full(n_min, "", dtype=object)
        for s, e in p.windows:
            top[int(s // 60): int(e // 60)] = p.priority[0]
        for a, b, r in p.overrides:
            seg = top[int(a // 60): int(b // 60)]
            seg[seg != ""] = r[0]
        for q in out:
            out[q] += top == q
    return out
