"""Phase 2 checkpoint: replay a normal day and the outage day, and draw docs/img/phase2_replay.png.

Run:  uv run python scripts/phase2_replay.py
"""

from __future__ import annotations

import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from calibrate import affected_sl, decision_time, strong_response
from phase1_overview import AXIS, INK_2, SERIES, SURFACE, _style
from wfm.config import ROOT
from wfm.data.scenarios import get_scenario, load_scenarios
from wfm.sim.day import prepare_scenario, run_day

OUT = ROOT / "docs" / "img" / "phase2_replay.png"
TYPE_LABEL = {"outage_spike": "Outage", "marketing_campaign": "Marketing", "weather_seasonal_surge": "Weather /\nseasonal",
              "sick_call_wave": "Sick-call\nwave", "aht_spike": "Slow\nsystems", "adversarial": "Adversarial"}


def main() -> None:
    timings = {}
    for sid in ("NORMAL-01", "OUT-01"):
        t = time.perf_counter()
        run_day(sid)
        timings[sid] = time.perf_counter() - t
    print("replay time:", {k: f"{v:.2f}s" for k, v in timings.items()})

    demo = get_scenario("OUT-01")
    t_dec = decision_time(demo)
    calm = run_day(prepare_scenario(demo.model_copy(update={"shocks": []})))
    none = run_day("OUT-01")
    fixed = run_day("OUT-01", strong_response(demo, t_dec))
    hours = 8 + 0.25 * np.arange(48) + 0.125

    def series(result, field):
        rows = [m for m in result.interval_metrics() if m.queue == "payments"]
        return np.array([getattr(m, field) if getattr(m, field) is not None else np.nan for m in rows], dtype=float)

    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5), facecolor=SURFACE)
    ticks = (range(8, 21, 2), [f"{h:02d}:00" for h in range(8, 21, 2)])

    ax = axes[0, 0]
    fc = np.array([f.calls for f in none.inputs.forecast.intervals if f.queue == "payments"])
    arrived = series(none, "offered") + series(none, "deflected")
    ax.plot(hours, fc, color=SERIES[0], linewidth=2)
    ax.plot(hours, arrived, color=SERIES[1], linewidth=2)
    ax.text(14.3, 112, "Morning forecast", color=INK_2, fontsize=8)
    ax.text(12.2, arrived[17] + 6, "Calls that actually arrived", color=INK_2, fontsize=8)
    _style(ax, "A. Demo day (6 Oct, payments): forecast vs reality",
           "Calls per 15 minutes, simulated caller by caller")
    ax.set_xlim(8, 20)
    ax.set_xticks(*ticks)

    ax = axes[0, 1]
    for i, (res, label) in enumerate([(calm, "Same day, no outage"), (none, "Outage, no action"),
                                      (fixed, f"Outage, strong response at {t_dec}")]):
        ax.plot(hours, series(res, "service_level_pct"), color=SERIES[i], linewidth=2, label=label)
    ax.axhline(80, color=AXIS, linewidth=1, linestyle="--")
    ax.text(8.1, 82, "80% promise", color=INK_2, fontsize=8)
    _style(ax, "B. Service level every 15 minutes (payments)",
           "% of callers answered within 20 seconds")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="center right")
    ax.set_xlim(8, 20)
    ax.set_ylim(0, 105)
    ax.set_xticks(*ticks)

    ax = axes[1, 0]
    for i, (res, label) in enumerate([(none, "Outage, no action"), (fixed, "Outage, strong response")]):
        ax.plot(hours, series(res, "abandoned"), color=SERIES[i + 1], linewidth=2, label=label)
    _style(ax, "C. Callers who gave up and hung up (payments)",
           f"Per 15 minutes. Whole day: {none.summary().by_queue['payments'].abandoned:,} with no action vs "
           f"{fixed.summary().by_queue['payments'].abandoned:,} with a response")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="upper right")
    ax.set_xlim(8, 20)
    ax.set_xticks(*ticks)

    ax = axes[1, 1]
    rows: dict[str, list[tuple[float, float, float]]] = {}
    for sc in load_scenarios():
        t = decision_time(sc)
        c = affected_sl(run_day(prepare_scenario(sc.model_copy(update={"shocks": []}))), sc, t)[0]
        n = affected_sl(run_day(sc.id), sc, t)[0]
        f = affected_sl(run_day(sc.id, strong_response(sc, t)), sc, t)[0]
        rows.setdefault(sc.type, []).append((c, n, f))
    types = list(TYPE_LABEL)
    x = np.arange(len(types))
    width = 0.26
    for i, label in enumerate(["Same day, no disruption", "No action", "Strong response"]):
        vals = [np.mean([r[i] for r in rows[t]]) for t in types]
        ax.bar(x + (i - 1) * (width + 0.02), vals, width, color=SERIES[i], label=label)
    ax.axhline(80, color=AXIS, linewidth=1, linestyle="--")
    _style(ax, "D. All 30 bad days, by type (affected queues, decision to close)",
           "Average service level; each type is bad without action and mostly rescuable")
    ax.set_xticks(x, [TYPE_LABEL[t] for t in types], fontsize=8)
    ax.set_ylim(0, 105)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="upper center", ncol=3, bbox_to_anchor=(0.5, -0.12))

    fig.tight_layout(h_pad=3, w_pad=3)
    fig.savefig(OUT, dpi=140, facecolor=SURFACE)
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
