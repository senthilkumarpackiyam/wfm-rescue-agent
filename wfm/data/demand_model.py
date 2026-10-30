"""Ground-truth demand: how many calls *really* arrive, interval by interval.

Used twice, with the same maths:
  * history.py draws 21 months of past days from it (what the forecaster learns from);
  * the simulator (Phase 2) draws each scenario day from it, then layers the shocks on top.

The forecaster never calls this module; it only sees the history it produced.
"""

from __future__ import annotations

from datetime import date as Date

import numpy as np

from wfm.config import load_centre
from wfm.timegrid import n_intervals

_PEAK_DAY_OF_YEAR = 320  # yearly wave peaks mid-November


def interval_midpoints_hours() -> np.ndarray:
    c = load_centre()
    step_h = c.interval_minutes / 60
    open_h = int(c.open_time[:2]) + int(c.open_time[3:]) / 60
    return open_h + step_h * (np.arange(n_intervals()) + 0.5)


def intraday_profile() -> np.ndarray:
    """Share of the day's calls landing in each interval (sums to 1)."""
    t = interval_midpoints_hours()
    shape = np.full_like(t, 0.12)  # floor so the edges of the day are never empty
    for centre, width, weight in load_centre().demand_model.intraday_bumps:
        shape += weight * np.exp(-0.5 * ((t - centre) / width) ** 2)
    return shape / shape.sum()


def intraday_aht_shape() -> np.ndarray:
    """Handle time drifts a little through the day (complex calls cluster mid-afternoon)."""
    t = interval_midpoints_hours()
    return 1.0 + 0.04 * np.exp(-0.5 * ((t - 15.5) / 2.0) ** 2) - 0.02 * np.exp(-0.5 * ((t - 9.0) / 1.0) ** 2)


def expected_daily_volume(day: Date, queue: str) -> float:
    """Noise-free daily volume: base x day-of-week x trend x yearly wave."""
    c = load_centre()
    dm = c.demand_model
    start = Date.fromisoformat(c.history.start_date)
    years = (day - start).days / 365.25
    trend = (1 + dm.annual_growth_pct / 100) ** years
    wave = 1 + dm.annual_seasonality_pct / 100 * np.cos(
        2 * np.pi * (day.timetuple().tm_yday - _PEAK_DAY_OF_YEAR) / 365.25
    )
    # weekday_daily_volume is calibrated to a Wednesday (multiplier 1.00)
    return c.queues[queue].weekday_daily_volume * dm.day_of_week[day.weekday()] * trend * wave


def day_rng(seed: int, day: Date, queue: str) -> np.random.Generator:
    """Independent, reproducible random stream per (seed, day, queue)."""
    return np.random.default_rng([seed, day.toordinal(), 0 if queue == "payments" else 1])


def true_day(day: Date, queue: str, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Expected calls and mean AHT per interval for one day, including that day's
    unforecastable noise. Returns (call_rate[interval], aht_sec[interval])."""
    c = load_centre()
    dm = c.demand_model
    q = c.queues[queue]
    day_factor = rng.lognormal(mean=-0.5 * dm.daily_noise_cv**2, sigma=dm.daily_noise_cv)
    aht_factor = rng.lognormal(mean=-0.5 * dm.aht_daily_noise_cv**2, sigma=dm.aht_daily_noise_cv)
    calls = expected_daily_volume(day, queue) * day_factor * intraday_profile()
    aht = q.aht_seconds * aht_factor * intraday_aht_shape()
    return calls, aht
