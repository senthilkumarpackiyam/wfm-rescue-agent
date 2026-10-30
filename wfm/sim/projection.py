"""The agent's what-if world: the real past up to `now`, an *imagined* future drawn from its
own re-forecast, and only the absences it already knows about.

Testing plans against the scenario's real future would be clairvoyance (the agent would
know exactly when an outage ends). Only the approved plan is run against reality.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from wfm.config import load_centre
from wfm.schemas import AppliedAction, OfferOvertime, Reforecast, Snapshot
from wfm.sim.calls import QUEUE_CODE, CallSpec
from wfm.sim.day import DayInputs
from wfm.timegrid import clock_to_sim


def _excess_mix(snap: Snapshot, queue: str, last_n: int = 2) -> dict[str, float] | None:
    """Reasons that are running above normal recently, as shares (None if nothing stands out)."""
    centre = load_centre()
    done = sorted({r.interval_start for r in snap.reason_codes if r.queue == queue})[-last_n:]
    counts = Counter()
    for r in snap.reason_codes:
        if r.queue == queue and r.interval_start in done:
            counts[r.reason] += r.count
    fc = {(f.queue, f.interval_start): f.calls for f in snap.forecast.intervals}
    expected = sum(fc[(queue, t)] for t in done)
    excess = {k: counts[k] - expected * p for k, p in centre.queues[queue].reason_mix.items()}
    excess = {k: v for k, v in excess.items() if v > 0}
    total = sum(excess.values())
    return {k: v / total for k, v in excess.items()} if total > 0.1 * max(expected, 1) else None


def project_inputs(real: DayInputs, snap: Snapshot, rf: Reforecast, applied: list[AppliedAction]) -> DayInputs:
    centre = load_centre()
    now_sec = clock_to_sim(snap.now) * 60
    rng = np.random.default_rng([real.scenario.seed, 777, clock_to_sim(snap.now)])
    past = [c for c in real.calls if c.arrival < now_sec]
    base_fc = {(f.queue, f.interval_start): f.calls for f in snap.forecast.intervals}
    future: list[tuple] = []
    for q in centre.queue_ids:
        qc = centre.queues[q]
        normal = qc.reason_mix
        extra_mix = _excess_mix(snap, q) or normal
        sigma2 = np.log(1 + qc.aht_cv**2)
        for f in (f for f in rf.forecast.intervals if f.queue == q):
            start = clock_to_sim(f.interval_start) * 60
            if start < now_sec:
                continue
            base = base_fc[(q, f.interval_start)]
            extra = max(0.0, f.calls - base)
            mix = Counter()
            for k, p in normal.items():
                mix[k] += min(base, f.calls) * p
            for k, p in extra_mix.items():
                mix[k] += extra * p
            codes = list(mix)
            probs = np.array([mix[k] for k in codes])
            probs = probs / probs.sum() if probs.sum() > 0 else np.full(len(codes), 1 / len(codes))
            n = rng.poisson(f.calls)
            arrival = start + rng.uniform(0, 900, n)
            reasons = np.array(codes, dtype=object)[rng.choice(len(codes), n, p=probs)]
            handle = rng.lognormal(np.log(f.aht_sec) - sigma2 / 2, np.sqrt(sigma2), n)
            patience = rng.exponential(qc.mean_patience_seconds, n)
            u = rng.random((3, n))
            future += [(arrival[i], q, reasons[i], handle[i], patience[i], u[0, i], u[1, i], u[2, i]) for i in range(n)]

    rows = [(c.arrival, c.queue, c.reason, c.handle, c.patience, c.u_ivr, c.u_chat, c.u_callback) for c in past] + future
    rows.sort(key=lambda r: r[0])
    calls = [CallSpec(i, r[1], float(r[0]), str(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]),
                      float(r[7])) for i, r in enumerate(rows)]
    known = {e.agent_id for e in snap.attendance}
    used = sum(a.action.agents_offered for a in applied if isinstance(a.action, OfferOvertime))
    ot = np.concatenate([real.ot_draws[:used], rng.random(1000)])  # past acceptances are fact; new ones unknown
    return DayInputs(
        real.scenario, real.forecast, real.roster, calls,
        {k: v for k, v in real.absent.items() if k in known}, list(snap.attendance), ot,
        arrival=np.array([c.arrival for c in calls]),
        queue_code=np.array([QUEUE_CODE[c.queue] for c in calls]),
        reason=np.array([c.reason for c in calls], dtype=object),
        handle=np.array([c.handle for c in calls]),
    )

