"""Generate synthetic interval history -> data/history/intervals.csv.

Run:  uv run python -m wfm.data.history
"""

from __future__ import annotations

from datetime import date as Date, timedelta

import numpy as np
import pandas as pd

from wfm.config import DATA_DIR, load_centre
from wfm.data.demand_model import day_rng, true_day
from wfm.timegrid import interval_labels

HISTORY_CSV = DATA_DIR / "history" / "intervals.csv"


def generate_history() -> pd.DataFrame:
    c = load_centre()
    start = Date.fromisoformat(c.history.start_date)
    end = Date.fromisoformat(c.history.end_date)
    labels = interval_labels()
    frames = []
    day = start
    while day <= end:
        for queue, qcfg in c.queues.items():
            rng = day_rng(c.history.seed, day, queue)
            rate, aht = true_day(day, queue, rng)
            offered = rng.poisson(rate)
            # Observed interval AHT is the mean of `offered` random handle times.
            sd = aht * qcfg.aht_cv / np.sqrt(np.maximum(offered, 1))
            observed_aht = np.maximum(30.0, rng.normal(aht, sd))
            frames.append(
                pd.DataFrame(
                    {
                        "date": day.isoformat(),
                        "interval_start": labels,
                        "queue": queue,
                        "offered": offered,
                        "aht_sec": np.round(observed_aht, 1),
                    }
                )
            )
        day += timedelta(days=1)
    return pd.concat(frames, ignore_index=True)


def load_history() -> pd.DataFrame:
    return pd.read_csv(HISTORY_CSV)


def main() -> None:
    df = generate_history()
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(HISTORY_CSV, index=False)
    daily = df.groupby(["date", "queue"])["offered"].sum().unstack()
    print(f"wrote {len(df):,} rows to {HISTORY_CSV.relative_to(DATA_DIR.parent)}")
    print(f"days: {daily.index.min()} .. {daily.index.max()} ({len(daily)})")
    print("mean daily calls per queue:", daily.mean().round(0).to_dict())


if __name__ == "__main__":
    main()
