"""Tune the detector: no alarms on calm days, fast alarms on bad days.

Run:  uv run python scripts/calibrate_detector.py
"""

from __future__ import annotations

import numpy as np

from wfm.agent.detect import DetectorConfig, detect
from wfm.data.scenarios import load_scenarios
from wfm.sim.day import prepare_scenario, run_day
from wfm.timegrid import hhmm_to_minutes, interval_labels

TICKS = interval_labels()[1:] + ["20:00"]  # first check at 08:15


def first_alert(result, cfg):
    for t in TICKS:
        alerts = detect(result.snapshot(t), cfg)
        if alerts:
            return t, alerts
    return None, []


def main(cfg: DetectorConfig = DetectorConfig()) -> None:
    scenarios = load_scenarios(include_control=True)
    false_days = []
    for sc in scenarios:
        calm = run_day(prepare_scenario(sc.model_copy(update={"shocks": []})))
        t, alerts = first_alert(calm, cfg)
        if t:
            false_days.append((sc.date, t, [(a.queue, a.metric, a.gap_pct) for a in alerts]))
    print(f"calm days with a false alarm: {len(false_days)} of {len(scenarios)}")
    for d in false_days:
        print("   ", d)

    delays, missed = [], []
    for sc in load_scenarios():
        t, alerts = first_alert(run_day(sc.id), cfg)
        if t is None:
            missed.append(sc.id)
            continue
        delay = hhmm_to_minutes(t) - hhmm_to_minutes(sc.shocks[0].start)
        delays.append(delay)
        print(f"{sc.id:7} shock {sc.shocks[0].start} alert {t} (+{delay:3d} min) "
              f"{sorted({(a.queue, a.metric) for a in alerts})}")
    print(f"missed: {missed}; median delay {np.median(delays):.0f} min, worst {max(delays)} min")


if __name__ == "__main__":
    main()
