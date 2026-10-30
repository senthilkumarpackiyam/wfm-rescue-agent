"""Morning forecast: SARIMA on daily totals, spread over the day by a recent intraday profile.

This is the classic WFM split: a time-series model predicts *how many* calls a day brings;
the average shape of recent same-weekday days says *when* in the day they arrive.

Run:  uv run python -m wfm.forecast.sarima     (fits, backtests, writes data/forecast/)
"""

from __future__ import annotations

import json
import warnings
from datetime import date as Date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd
from statsmodels.tsa.statespace.sarimax import SARIMAX

from wfm.config import DATA_DIR, load_centre
from wfm.data.history import load_history
from wfm.schemas import DayForecast, IntervalForecast
from wfm.timegrid import interval_labels

FORECAST_DIR = DATA_DIR / "forecast"
DAILY_CSV = FORECAST_DIR / "daily_forecast.csv"
BACKTEST_JSON = FORECAST_DIR / "backtest.json"

ORDER = (0, 1, 1)  # "airline model": best September backtest of four candidates
SEASONAL_ORDER = (0, 1, 1, 7)  # weekly rhythm, differenced once
TRAIN_DAYS = 364
PROFILE_WEEKS = 8
HORIZON_END = Date(2026, 11, 30)


@lru_cache
def _history() -> pd.DataFrame:
    df = load_history()
    df["date"] = pd.to_datetime(df["date"])
    return df


def _daily_series(queue: str, train_end: Date) -> pd.Series:
    df = _history()
    s = df[(df["queue"] == queue) & (df["date"] <= pd.Timestamp(train_end))]
    s = s.groupby("date")["offered"].sum().asfreq("D")
    return s.iloc[-TRAIN_DAYS:]


def forecast_daily(queue: str, train_end: Date, horizon_end: Date) -> pd.DataFrame:
    """Daily calls from train_end+1 to horizon_end with an 80% prediction interval."""
    y = np.log(_daily_series(queue, train_end))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = SARIMAX(y, order=ORDER, seasonal_order=SEASONAL_ORDER, trend="n").fit(disp=False)
    steps = (horizon_end - train_end).days
    fc = fit.get_forecast(steps=steps)
    ci = fc.conf_int(alpha=0.2)
    return pd.DataFrame({
        "date": [d.date().isoformat() for d in fc.predicted_mean.index],
        "queue": queue,
        "calls": np.exp(fc.predicted_mean.values).round(1),
        "calls_low": np.exp(ci.iloc[:, 0].values).round(1),
        "calls_high": np.exp(ci.iloc[:, 1].values).round(1),
    })


def intraday_shape(queue: str, day: Date, train_end: Date) -> tuple[np.ndarray, np.ndarray]:
    """Share of calls per interval and mean AHT per interval, from the last 8 same weekdays."""
    df = _history()
    recent = df[(df["queue"] == queue) & (df["date"] <= pd.Timestamp(train_end))
                & (df["date"].dt.day_of_week == day.weekday())]
    recent = recent[recent["date"] >= recent["date"].max() - pd.Timedelta(weeks=PROFILE_WEEKS)]
    by_slot = recent.groupby("interval_start").apply(
        lambda g: pd.Series({"calls": g["offered"].sum(),
                             "aht": np.average(g["aht_sec"], weights=g["offered"].clip(lower=1))}),
        include_groups=False,
    ).reindex(interval_labels())
    share = by_slot["calls"].values / by_slot["calls"].sum()
    return share, by_slot["aht"].values


@lru_cache
def daily_forecast_table() -> pd.DataFrame:
    return pd.read_csv(DAILY_CSV)


def day_forecast(day: Date) -> DayForecast:
    """The morning plan's forecast for one day (issued from history ending 30 Sep 2026)."""
    train_end = Date.fromisoformat(load_centre().history.end_date)
    table = daily_forecast_table()
    intervals = []
    for queue in load_centre().queue_ids:
        row = table[(table["date"] == day.isoformat()) & (table["queue"] == queue)].iloc[0]
        share, aht = intraday_shape(queue, day, train_end)
        for label, s, a in zip(interval_labels(), share, aht):
            intervals.append(IntervalForecast(
                interval_start=label, queue=queue, calls=round(float(row.calls * s), 2),
                aht_sec=round(float(a), 1),
                calls_low=round(float(row.calls_low * s), 2), calls_high=round(float(row.calls_high * s), 2),
            ))
    return DayForecast(date=day.isoformat(), made_at="07:00", method="sarima_daily+intraday_profile",
                       intervals=intervals)


def backtest(train_end: Date = Date(2026, 8, 31), test_end: Date = Date(2026, 9, 30)) -> dict:
    """Fit on data to train_end, forecast the following month, score against what happened."""
    df = _history()
    out = {"train_end": train_end.isoformat(), "test_end": test_end.isoformat(), "queues": {}}
    for queue in load_centre().queue_ids:
        fc = forecast_daily(queue, train_end, test_end)
        actual = (df[(df["queue"] == queue) & (df["date"] > pd.Timestamp(train_end))]
                  .groupby("date")["offered"].sum())
        a = actual.values[: len(fc)]
        f = fc["calls"].values
        inside = ((a >= fc["calls_low"].values) & (a <= fc["calls_high"].values)).mean()
        # Interval-level accuracy: daily forecast x intraday profile vs actual 15-min counts.
        errs, tot = 0.0, 0.0
        for d, daily_calls in zip(fc["date"], f):
            day = Date.fromisoformat(d)
            share, _ = intraday_shape(queue, day, train_end)
            act = df[(df["queue"] == queue) & (df["date"] == pd.Timestamp(day))]["offered"].values
            errs += np.abs(act - daily_calls * share).sum()
            tot += act.sum()
        out["queues"][queue] = {
            "daily_mape_pct": round(float(np.mean(np.abs(a - f) / a) * 100), 2),
            "interval_wape_pct": round(float(errs / tot * 100), 2),
            "pi80_coverage_pct": round(float(inside * 100), 1),
        }
    return out


def main() -> None:
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    bt = backtest()
    BACKTEST_JSON.write_text(json.dumps(bt, indent=2))
    print("backtest (train to 2026-08-31, test Sep 2026):", json.dumps(bt["queues"]))
    train_end = Date.fromisoformat(load_centre().history.end_date)
    table = pd.concat([forecast_daily(q, train_end, HORIZON_END) for q in load_centre().queue_ids])
    table.to_csv(DAILY_CSV, index=False)
    print(f"wrote daily forecast {train_end + timedelta(days=1)} .. {HORIZON_END} to {DAILY_CSV.name}")


if __name__ == "__main__":
    main()
