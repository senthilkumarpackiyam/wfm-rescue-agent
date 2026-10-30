"""Walk a scenario day every 15 sim-minutes and run the agent graph until it first acts.

Phase 3 stops at the first alert (detect -> diagnose -> reforecast). Phase 4 extends the
loop with planning, approval, execution and monitoring for the rest of the day.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from wfm.agent.diagnose import DiagnosisRun
from wfm.agent.graph import build_graph
from wfm.agent.llm import LLM
from wfm.config import ROOT
from wfm.schemas import Alert, DecisionLogEntry, Reforecast
from wfm.sim.day import run_day
from wfm.timegrid import interval_labels

RUNS_DIR = ROOT / "runs"
TICKS = interval_labels()[1:] + ["20:00"]  # 08:15 ... 20:00


@dataclass
class AgentRun:
    scenario_id: str
    alert_at: str | None = None
    alerts: list[Alert] = field(default_factory=list)
    diagnosis_run: DiagnosisRun | None = None
    reforecast: Reforecast | None = None
    log: list[DecisionLogEntry] = field(default_factory=list)
    seconds: float = 0.0


def first_response(scenario_id: str, llm: LLM) -> AgentRun:
    t0 = time.perf_counter()
    day = run_day(scenario_id)
    graph = build_graph(llm, plan=False)
    run = AgentRun(scenario_id)
    for tick in TICKS:
        state = graph.invoke({"snapshot": day.snapshot(tick), "log": []})
        if state["alerts"]:
            run.alert_at, run.alerts = tick, state["alerts"]
            run.diagnosis_run, run.reforecast = state["diagnosis_run"], state["reforecast"]
            run.log = state["log"]
            break
    run.seconds = round(time.perf_counter() - t0, 1)
    return run


def save_log(run: AgentRun) -> str:
    folder = RUNS_DIR / run.scenario_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    path.write_text("".join(json.dumps(e.model_dump()) + "\n" for e in run.log))
    return str(path.relative_to(ROOT))
