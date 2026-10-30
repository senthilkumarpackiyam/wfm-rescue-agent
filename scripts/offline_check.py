"""Phase 7: prove the demo runs with no API key and no Claude connection.

    uv run python scripts/offline_check.py

The key is removed, `.env` is ignored, and creating an Anthropic client raises, so any attempt
to reach Claude fails loudly. Then:
  1. the pitch path on OUT-01 (reject "no overtime budget today", approve the rest), and
  2. every day in the dashboard's picker, approving each recommendation (the "crowd moment"),
must all finish from saved answers alone.
"""

from __future__ import annotations

import os
import sys
import time

import anthropic

import wfm.config
from wfm.agent import llm as llm_module
from wfm.ui.controller import DemoController, scenario_choices


class NoNetwork(anthropic.Anthropic):
    def __init__(self, *args, **kwargs):
        raise RuntimeError("offline check: something tried to create an Anthropic client")


def isolate() -> None:
    os.environ.pop("ANTHROPIC_API_KEY", None)
    wfm.config.load_env = lambda: None
    llm_module.load_env = lambda: None
    llm_module.anthropic.Anthropic = NoNetwork


def play(scenario_id: str, reject_with: str | None = None) -> tuple[DemoController, float]:
    t0 = time.perf_counter()
    c = DemoController(scenario_id, llm_mode="cached", start="09:45")
    c.next_event()
    if reject_with and c.waiting:
        c.reject(reject_with)
    for _ in range(20):
        if c.finished or c.session.llm_errors:
            break
        if c.waiting:
            c.approve()
        else:
            c.run_to_end()
    return c, time.perf_counter() - t0


def report(label: str, c: DemoController, seconds: float) -> bool:
    ok = c.finished and not c.session.llm_errors and c.llm.usage.calls == 0
    since = c.session.first_alert or "08:00"
    before, after = c.baseline.summary(since), c.session.outcome()
    print(f"{'ok  ' if ok else 'FAIL'} {label:34} SL {before.service_level_pct:5.1f}% -> {after.service_level_pct:5.1f}%  "
          f"saved answers {c.llm.replayed.calls:3}  live calls {c.llm.usage.calls}  {seconds:5.1f} s"
          + (f"  ERROR {c.session.llm_errors[0]}" if c.session.llm_errors else ""), flush=True)
    return ok


def main() -> None:
    isolate()
    failures = []
    c, s = play("OUT-01", reject_with="no overtime budget today")
    if not report("OUT-01 pitch path (reject overtime)", c, s):
        failures.append("OUT-01 pitch path")
    for sid in scenario_choices():
        c, s = play(sid)
        if not report(f"{sid} approve everything", c, s):
            failures.append(sid)
    print("offline check passed: no key, no connection, every day finished"
          if not failures else f"offline check FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
