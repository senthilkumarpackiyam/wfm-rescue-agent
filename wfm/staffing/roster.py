"""Morning staffing plan: forecast -> agents needed (Erlang A) -> shifts, breaks, coaching.

The plan knows nothing about the shocks; it is what a planner would publish the day before.

Run:  uv run python -m wfm.staffing.roster     (writes data/plans/<date>/ for every scenario day)
"""

from __future__ import annotations

from datetime import date as Date
from functools import lru_cache

import numpy as np

from wfm.config import DATA_DIR, load_centre
from wfm.forecast.sarima import day_forecast
from wfm.schemas import Agent, BreakSlot, DayForecast, Roster, Session
from wfm.staffing.erlang import required_agents
from wfm.timegrid import n_intervals, sim_to_clock

PLANS_DIR = DATA_DIR / "plans"
STEP = 15  # minutes per interval


def requirements(forecast: DayForecast) -> dict[str, np.ndarray]:
    """Agents needed on the phones per interval, by Erlang A at the planning target."""
    c = load_centre()
    target = c.staffing.planning_sl_target_pct / 100
    need = {q: np.zeros(n_intervals(), dtype=int) for q in c.queue_ids}
    labels = {sim_to_clock(i * STEP): i for i in range(n_intervals())}
    for f in forecast.intervals:
        need[f.queue][labels[f.interval_start]] = required_agents(
            f.calls, f.aht_sec, c.queues[f.queue].mean_patience_seconds, target,
            c.service_level.threshold_seconds)
    return need


def _shift_candidates() -> list[tuple[int, int, str]]:
    """(start interval, length in intervals, kind). Full-timers start 08:00-11:30, part-timers to 15:30."""
    s = load_centre().staffing
    ft, pt = s.full_time_shift_minutes // STEP, s.part_time_shift_minutes // STEP
    cands = [(i, ft, "FT") for i in range(0, n_intervals() - ft + 1, 2)]
    cands += [(i, pt, "PT") for i in range(0, n_intervals() - pt + 1, 2)]
    return cands


def _plan_shifts(target: np.ndarray) -> list[tuple[int, int, str]]:
    """Greedy cover: keep adding the shift that fills the most remaining gap per paid hour."""
    s = load_centre().staffing
    on_phone = {"FT": (s.full_time_shift_minutes - 60) / s.full_time_shift_minutes,
                "PT": (s.part_time_shift_minutes - 15) / s.part_time_shift_minutes}
    preference = {"FT": 1.0, "PT": 0.85}
    cover = np.zeros(n_intervals())
    shifts = []
    while (target - cover).max() > 0.3:
        gap = np.clip(target - cover, 0, None)
        best, best_score = None, 0.0
        for start, length, kind in _shift_candidates():
            filled = np.minimum(gap[start:start + length], on_phone[kind]).sum()
            score = filled / length * preference[kind]
            if score > best_score:
                best, best_score = (start, length, kind), score
        if best is None:
            break
        shifts.append(best)
        cover[best[0]:best[0] + best[1]] += on_phone[best[2]]
    return shifts


def _place(cover: np.ndarray, need: np.ndarray, lo: int, hi: int, length: int) -> int:
    """Start interval in [lo, hi] for an off-phone block that hurts coverage least."""
    best, best_key = lo, None
    for start in range(lo, hi + 1):
        surplus = (cover[start:start + length] - need[start:start + length]).min()
        key = (surplus, -abs(start - (lo + hi) / 2))
        if best_key is None or key > best_key:
            best, best_key = start, key
    cover[best:best + length] -= 1
    return best


def build_roster(forecast: DayForecast) -> Roster:
    c = load_centre()
    s = c.staffing
    day = Date.fromisoformat(forecast.date)
    rng = np.random.default_rng([s.roster_seed, day.toordinal()])
    need = requirements(forecast)
    absence_uplift = 1 / (1 - s.normal_absence_pct / 100)
    agents: list[Agent] = []
    session_no = 0
    for queue in c.queue_ids:
        shifts = sorted(_plan_shifts(need[queue] * absence_uplift))
        cover = np.zeros(n_intervals())
        for start, length, _ in shifts:
            cover[start:start + length] += 1
        prefix = "PAY" if queue == "payments" else "GEN"
        q_agents = []
        for k, (start, length, kind) in enumerate(shifts, 1):
            breaks = []
            if kind == "FT":
                plan = [(8, 1, "break"), (16, 2, "lunch"), (26, 1, "break")]
            else:
                plan = [(8, 1, "break")]
            for offset, dur, bkind in plan:
                b = _place(cover, need[queue], start + offset - 2, start + offset + 2, dur)
                breaks.append(BreakSlot(start=sim_to_clock(b * STEP), duration_min=dur * STEP, kind=bkind))
            q_agents.append(dict(id=f"{prefix}-{k:03d}", start=start, length=length, breaks=breaks))

        # Off-phone development time, parked where the queue has the most slack.
        order = rng.permutation(len(q_agents))
        n_comp = int(round(len(q_agents) * s.compliance_share))
        n_coach = int(round(len(q_agents) * s.coaching_share))
        for rank, idx in enumerate(order[: n_comp + n_coach]):
            a = q_agents[idx]
            mandatory = rank < n_comp
            dur = 4 if mandatory else 2
            busy = {int((int(b.start[:2]) * 60 + int(b.start[3:]) - 480) // STEP) + j
                    for b in a["breaks"] for j in range(b.duration_min // STEP)}
            options = [t for t in range(a["start"] + 2, a["start"] + a["length"] - dur - 1)
                       if not busy & set(range(t, t + dur))]
            if not options:
                continue
            t = max(options, key=lambda t: (cover[t:t + dur] - need[queue][t:t + dur]).min())
            cover[t:t + dur] -= 1
            session_no += 1
            a["sessions"] = [Session(
                id=f"{'COMP' if mandatory else 'COACH'}-{session_no:03d}", start=sim_to_clock(t * STEP),
                duration_min=dur * STEP, kind="compliance_training" if mandatory else "coaching",
                mandatory=mandatory)]

        cross = set(rng.choice(len(q_agents), size=int(round(len(q_agents) * s.cross_skilled_pct / 100)),
                               replace=False))
        other = [q for q in c.queue_ids if q != queue]
        for idx, a in enumerate(q_agents):
            agents.append(Agent(
                id=a["id"], primary_queue=queue, skills=[queue] + (other if idx in cross else []),
                shift_start=sim_to_clock(a["start"] * STEP),
                shift_end=sim_to_clock((a["start"] + a["length"]) * STEP),
                breaks=a["breaks"], sessions=a.get("sessions", []),
            ))
    return Roster(date=forecast.date, agents=agents)


def on_phone_coverage(roster: Roster, queue: str) -> np.ndarray:
    """Planned agents on the phones per interval (shift minus breaks and sessions)."""
    cover = np.zeros(n_intervals())

    def idx(hhmm: str) -> int:
        return (int(hhmm[:2]) * 60 + int(hhmm[3:]) - 480) // STEP

    for a in roster.agents:
        if a.primary_queue != queue:
            continue
        cover[idx(a.shift_start):idx(a.shift_end)] += 1
        for block in [*a.breaks, *a.sessions]:
            cover[idx(block.start):idx(block.start) + block.duration_min // STEP] -= 1
    return cover


@lru_cache(maxsize=64)
def morning_plan(day_iso: str) -> tuple[DayForecast, Roster]:
    """Forecast + roster for a day, read from data/plans if already built."""
    folder = PLANS_DIR / day_iso
    if (folder / "roster.json").exists():
        return (DayForecast.model_validate_json((folder / "forecast.json").read_text()),
                Roster.model_validate_json((folder / "roster.json").read_text()))
    forecast = day_forecast(Date.fromisoformat(day_iso))
    return forecast, build_roster(forecast)


def main() -> None:
    from wfm.data.scenarios import load_heldout, load_scenarios

    for sc in [*load_scenarios(include_control=True), *load_heldout()]:
        forecast = day_forecast(Date.fromisoformat(sc.date))
        roster = build_roster(forecast)
        folder = PLANS_DIR / sc.date
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "forecast.json").write_text(forecast.model_dump_json(indent=1))
        (folder / "roster.json").write_text(roster.model_dump_json(indent=1))
        n = {q: sum(a.primary_queue == q for a in roster.agents) for q in load_centre().queue_ids}
        print(f"{sc.id:10} {sc.date}  agents {n}")


if __name__ == "__main__":
    main()
