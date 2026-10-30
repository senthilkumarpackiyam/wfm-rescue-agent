"""Clock helpers. Simulated time is measured in minutes since the centre opens."""

from __future__ import annotations

from wfm.config import load_centre


def hhmm_to_minutes(hhmm: str) -> int:
    """'10:20' -> 620 (minutes since midnight)."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(minutes: float) -> str:
    """620 -> '10:20'."""
    total = int(round(minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


def open_minutes() -> int:
    return hhmm_to_minutes(load_centre().open_time)


def day_length_minutes() -> int:
    c = load_centre()
    return hhmm_to_minutes(c.close_time) - hhmm_to_minutes(c.open_time)


def n_intervals() -> int:
    return day_length_minutes() // load_centre().interval_minutes


def clock_to_sim(hhmm: str) -> int:
    """Wall-clock 'HH:MM' -> sim minutes since open ('10:20' -> 140 when open at 08:00)."""
    return hhmm_to_minutes(hhmm) - open_minutes()


def sim_to_clock(sim_minutes: float) -> str:
    return minutes_to_hhmm(sim_minutes + open_minutes())


def interval_index(sim_minutes: float) -> int:
    """Which 15-minute interval a sim time falls in (clamped to the day)."""
    idx = int(sim_minutes // load_centre().interval_minutes)
    return max(0, min(idx, n_intervals() - 1))


def interval_labels() -> list[str]:
    step = load_centre().interval_minutes
    return [sim_to_clock(i * step) for i in range(n_intervals())]
