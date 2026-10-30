"""Draw docs/img/phase1_overview.png: what the synthetic history and the bad days look like.

Run:  uv run python scripts/phase1_overview.py
"""

from __future__ import annotations

from datetime import date as Date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from wfm.config import ROOT, load_centre
from wfm.data.demand_model import expected_daily_volume, intraday_profile
from wfm.data.history import load_history
from wfm.data.scenarios import get_scenario
from wfm.data.shocks import shock_multiplier
from wfm.timegrid import interval_labels, open_minutes

# Reference palette (light mode): categorical slots in fixed order, ink and chrome.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
OUT = ROOT / "docs" / "img" / "phase1_overview.png"


def _style(ax, title: str, subtitle: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=12, color=INK, fontweight="bold", pad=22)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color=INK_2, va="bottom")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)


def main() -> None:
    c = load_centre()
    hist = load_history()
    labels = interval_labels()
    hours = np.array([open_minutes() + 15 * i + 7.5 for i in range(len(labels))]) / 60
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5), facecolor=SURFACE)

    # A. 21 months of daily calls
    ax = axes[0, 0]
    daily = hist.groupby(["date", "queue"])["offered"].sum().unstack()
    daily.index = pd.to_datetime(daily.index)
    for i, q in enumerate(c.queue_ids):
        smooth = daily[q].rolling(28, center=True, min_periods=7).mean()
        ax.plot(daily.index, daily[q], color=SERIES[i], linewidth=0.6, alpha=0.3)
        ax.plot(daily.index, smooth, color=SERIES[i], linewidth=2)
        ax.text(daily.index[-1], smooth.dropna().iloc[-1], f"  {c.queues[q].label}",
                color=INK_2, fontsize=8, va="center")
    _style(ax, "A. Our fake history: calls per day, Jan 2025 - Sep 2026",
           "Faint zig-zag = weekly rhythm (busy Mondays, quiet Sundays); bold line = 4-week average")
    ax.set_xlim(daily.index[0], daily.index[-1] + pd.Timedelta(days=120))

    # B. Shape of a normal day
    ax = axes[0, 1]
    weds = hist[pd.to_datetime(hist["date"]).dt.day_of_week == 2]
    curve = weds.groupby(["interval_start", "queue"])["offered"].mean().unstack()
    for i, q in enumerate(c.queue_ids):
        ax.plot(hours, curve[q].values, color=SERIES[i], linewidth=2)
        ax.text(hours[-1] + 0.1, curve[q].values[-1], c.queues[q].label, color=INK_2, fontsize=8, va="center")
    _style(ax, "B. A normal Wednesday: calls per 15 minutes",
           "Two busy humps - late morning and mid-afternoon")
    ax.set_xlim(8, 21.8)
    ax.set_xticks(range(8, 21, 2), [f"{h:02d}:00" for h in range(8, 21, 2)])

    # C. Demo day: forecast vs reality
    ax = axes[1, 0]
    demo = get_scenario("OUT-01")
    day = Date.fromisoformat(demo.date)
    normal = expected_daily_volume(day, "payments") * intraday_profile()
    actual = normal * shock_multiplier(demo.shocks[0], hours * 60)
    ax.plot(hours, normal, color=SERIES[0], linewidth=2)
    ax.plot(hours, actual, color=SERIES[1], linewidth=2)
    ax.text(11.5, normal[14] - 6, "What the plan expects", color=INK_2, fontsize=8, va="top")
    ax.text(13.0, 222, "What actually arrives", color=INK_2, fontsize=8)
    ax.axvline(10 + 20 / 60, color=AXIS, linewidth=1, linestyle="--")
    ax.text(10 + 20 / 60 - 0.1, ax.get_ylim()[1] * 0.92, "10:20  UPI outage", color=INK_2,
            fontsize=8, ha="right")
    _style(ax, "C. Demo day OUT-01 (payments queue): plan vs reality",
           "Expected calls per 15 min, before any random noise")
    ax.set_xlim(8, 20)
    ax.set_xticks(range(8, 21, 2), [f"{h:02d}:00" for h in range(8, 21, 2)])

    # D. Five kinds of bad day
    ax = axes[1, 1]
    examples = [("OUT-01", "Outage: sharp spike in calls"),
                ("MKT-01", "Marketing: long planned hump"),
                ("WTH-01", "Weather: higher all day, no clue why"),
                ("AHT-01", "Slow systems: calls take longer"),
                ("SCK-05", "Sick wave: fewer agents after lunch")]
    minute_grid = np.arange(8 * 60, 20 * 60 + 1)
    for i, (sid, label) in enumerate(examples):
        ax.plot(minute_grid / 60, shock_multiplier(get_scenario(sid).shocks[0], minute_grid),
                color=SERIES[i], linewidth=2, label=label)
    ax.axhline(1.0, color=AXIS, linewidth=1)
    _style(ax, "D. Five kinds of bad day, as 'times normal'",
           "Above 1 = more calls or longer calls; below 1 = fewer agents")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="upper right")
    ax.set_xlim(8, 20)
    ax.set_ylim(0.6, 2.4)
    ax.set_xticks(range(8, 21, 2), [f"{h:02d}:00" for h in range(8, 21, 2)])

    fig.tight_layout(h_pad=3, w_pad=3)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=140, facecolor=SURFACE)
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
