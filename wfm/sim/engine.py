"""Discrete-event contact centre (SimPy).

Callers arrive at their pre-drawn times and join their queue. A free agent takes the
oldest waiting caller from the first queue in its routing order. A caller who has waited
longer than their patience has hung up. Agents finish the call they are on before a break.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import simpy

from wfm.config import load_centre, load_levers
from wfm.sim.calls import CallSpec
from wfm.sim.levers import DEFLECTED_CHAT, DEFLECTED_IVR, AgentPlan, Execution

PENDING, ANSWERED, ABANDONED, CALLBACK = 0, 1, 2, 5
assert DEFLECTED_IVR == 3 and DEFLECTED_CHAT == 4


@dataclass
class _AgentState:
    plan: AgentPlan
    wake: simpy.Event | None = None
    job: tuple[CallSpec, bool] | None = None  # (call, is_callback)
    idle_since: float = 0.0


@dataclass
class EngineOutput:
    status: np.ndarray  # per call: PENDING/ANSWERED/ABANDONED/DEFLECTED_*/CALLBACK
    answered_at: np.ndarray  # seconds since open (nan if not answered)
    ended_at: np.ndarray  # hang-up time for abandons, call end for answered
    callback_served_at: np.ndarray
    busy: list[tuple[str, float, float]]  # (queue served, start, end) per handled contact


class Engine:
    def __init__(self, calls: list[CallSpec], execution: Execution, staffed: dict[str, np.ndarray]):
        self.env = simpy.Environment()
        self.calls = calls
        self.ex = execution
        self.staffed = staffed
        n = len(calls)
        self.status = np.zeros(n, dtype=np.int8)
        for idx, code in execution.deflected.items():
            self.status[idx] = code
        self.answered_at = np.full(n, np.nan)
        self.ended_at = np.full(n, np.nan)
        self.cb_served = np.full(n, np.nan)
        queues = load_centre().queue_ids
        self.waiting: dict[str, deque[CallSpec]] = {q: deque() for q in queues}
        self.callbacks: dict[str, deque[CallSpec]] = {q: deque() for q in queues}
        self.idle: dict[str, _AgentState] = {}
        self.busy: list[tuple[str, float, float]] = []
        self.aht = {q: load_centre().queues[q].aht_seconds for q in queues}
        cb = load_levers()["ivr_callback"]["limits"]
        self.cb_wait, self.cb_accept = cb["callback_min_predicted_wait_seconds"], cb["callback_acceptance_rate"]

    # --- queue helpers -------------------------------------------------------------------

    def _pop_live(self, q: str, now: float) -> CallSpec | None:
        dq = self.waiting[q]
        while dq:
            c = dq.popleft()
            if c.arrival + c.patience <= now:
                self.status[c.idx] = ABANDONED
                self.ended_at[c.idx] = c.arrival + c.patience
            else:
                return c
        return None

    def _next_job(self, plan: AgentPlan, now: float) -> tuple[CallSpec, bool] | None:
        routing = plan.routing(now)
        for q in routing:
            c = self._pop_live(q, now)
            if c:
                return c, False
        for q in routing:
            if self.callbacks[q]:
                return self.callbacks[q].popleft(), True
        return None

    def _offers_callback(self, c: CallSpec) -> bool:
        if c.u_callback >= self.cb_accept or c.reason == "fraud_report":  # CX-04: fraud always reaches an agent
            return False
        for q, s, e in self.ex.callback_windows:
            if q == c.queue and s <= c.arrival < e:
                agents = max(1.0, self.staffed[q][int(c.arrival // 60)])
                return (len(self.waiting[q]) + 1) * self.aht[q] / agents > self.cb_wait
        return False

    # --- processes -------------------------------------------------------------------------

    def _serve(self, st: _AgentState, job: tuple[CallSpec, bool]):
        call, is_cb = job
        start = self.env.now
        if is_cb:
            self.cb_served[call.idx] = start
        else:
            self.status[call.idx] = ANSWERED
            self.answered_at[call.idx] = start
        yield self.env.timeout(call.handle)
        if not is_cb:
            self.ended_at[call.idx] = self.env.now
        self.busy.append((call.queue, start, self.env.now))

    def _agent(self, st: _AgentState):
        env = self.env
        for t0, t1 in st.plan.windows:
            if env.now < t0:
                yield env.timeout(t0 - env.now)
            while env.now < t1:
                job = self._next_job(st.plan, env.now)
                if job is None:
                    st.wake, st.job, st.idle_since = env.event(), None, env.now
                    self.idle[st.plan.id] = st
                    yield st.wake | env.timeout(t1 - env.now)
                    self.idle.pop(st.plan.id, None)
                    job, st.job = st.job, None
                    if job is None:
                        continue
                yield from self._serve(st, job)

    def _find_idle(self, q: str, now: float) -> _AgentState | None:
        best, best_key = None, None
        for st in self.idle.values():
            routing = st.plan.routing(now)
            if q in routing:
                key = (routing.index(q), st.idle_since)
                if best_key is None or key < best_key:
                    best, best_key = st, key
        return best

    def _arrivals(self):
        env = self.env
        for c in self.calls:
            if c.arrival > env.now:
                yield env.timeout(c.arrival - env.now)
            if self.status[c.idx] in (DEFLECTED_IVR, DEFLECTED_CHAT):
                continue
            if self._offers_callback(c):
                self.status[c.idx] = CALLBACK
                self.callbacks[c.queue].append(c)
                continue
            st = self._find_idle(c.queue, env.now)
            if st is not None:
                self.idle.pop(st.plan.id)
                st.job = (c, False)
                st.wake.succeed()
            else:
                self.waiting[c.queue].append(c)

    def run(self) -> EngineOutput:
        for plan in self.ex.agents:
            if plan.windows:
                self.env.process(self._agent(_AgentState(plan)))
        self.env.process(self._arrivals())
        self.env.run()
        for q in self.waiting:  # anyone still queued when the lights go out hangs up
            for c in self.waiting[q]:
                self.status[c.idx] = ABANDONED
                self.ended_at[c.idx] = c.arrival + c.patience
        return EngineOutput(self.status, self.answered_at, self.ended_at, self.cb_served, self.busy)
