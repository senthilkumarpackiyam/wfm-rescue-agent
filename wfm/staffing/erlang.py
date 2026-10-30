"""Erlang A (M/M/n+M): staffing maths with impatient callers. Pure code, never the LLM.

Model: Poisson arrivals (rate lambda), exponential handle times (rate mu = 1/AHT),
n agents, and exponential caller patience (rate theta = 1/mean patience).
The number of callers in the system is a birth-death chain we solve exactly; service
level comes from the transient behaviour of a tagged caller who finds j callers ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy.linalg import expm
from scipy.special import gammaln

INTERVAL_SECONDS = 900


@dataclass(frozen=True)
class ErlangResult:
    agents: int
    service_level: float  # P(answered within threshold), as a share of offered calls
    p_wait: float  # P(caller has to queue at all)
    p_abandon: float  # P(caller hangs up before being answered)
    avg_wait_sec: float  # mean time in queue across all callers
    occupancy: float  # share of agent time spent on calls


def _state_probs(lam: float, mu: float, theta: float, n: int) -> np.ndarray:
    """Stationary distribution of callers in the system, truncated where negligible."""
    a = lam / mu
    log_p = [k * np.log(a) - gammaln(k + 1) for k in range(n + 1)] if a > 0 else [0.0] + [-np.inf] * n
    k = n
    while True:
        k += 1
        log_p.append(log_p[-1] + np.log(lam / (n * mu + (k - n) * theta)))
        if k - n > 10 and log_p[-1] < max(log_p) - 35:
            break
        if k - n > 2000:
            break
    log_p = np.array(log_p)
    p = np.exp(log_p - log_p.max())
    return p / p.sum()


@lru_cache(maxsize=20_000)
def _p_served_within(n: int, mu: float, theta: float, t: float, max_ahead: int) -> np.ndarray:
    """P(tagged caller is answered within t | j callers ahead), for j = 0..max_ahead.

    States: j ahead (0..J), then 'served', then 'abandoned'. From j, the queue ahead
    shrinks at rate n*mu + j*theta; the tagged caller gives up at rate theta.
    """
    size = max_ahead + 3
    served, abandoned = max_ahead + 1, max_ahead + 2
    q = np.zeros((size, size))
    for j in range(max_ahead + 1):
        move = n * mu + j * theta
        q[j, served if j == 0 else j - 1] += move
        q[j, abandoned] += theta
        q[j, j] = -(move + theta)
    return expm(q * t)[: max_ahead + 1, served]


def erlang_a(calls: float, aht_sec: float, patience_sec: float, agents: int,
             threshold_sec: float = 20, interval_sec: float = INTERVAL_SECONDS) -> ErlangResult:
    """Expected performance of one interval with a fixed number of agents."""
    if calls <= 0:
        return ErlangResult(agents, 1.0, 0.0, 0.0, 0.0, 0.0)
    if agents <= 0:
        return ErlangResult(0, 0.0, 1.0, 1.0, patience_sec, 0.0)
    lam, mu, theta = calls / interval_sec, 1 / aht_sec, 1 / patience_sec
    p = _state_probs(lam, mu, theta, agents)
    n = agents
    p_wait = float(p[n:].sum())
    lq = float(np.dot(np.arange(len(p) - n), p[n:]))
    p_abandon = theta * lq / lam
    max_ahead = len(p) - n - 1
    served = _p_served_within(n, round(mu, 9), round(theta, 9), float(threshold_sec), max_ahead)
    sl = float(p[:n].sum() + np.dot(p[n:], served))
    busy = float(np.dot(np.minimum(np.arange(len(p)), n), p))
    return ErlangResult(n, sl, p_wait, p_abandon, lq / lam, busy / n)


def required_agents(calls: float, aht_sec: float, patience_sec: float, target_sl: float,
                    threshold_sec: float = 20, interval_sec: float = INTERVAL_SECONDS) -> int:
    """Fewest agents whose expected service level meets the target (target as 0-1)."""
    if calls <= 0:
        return 0
    load = calls * aht_sec / interval_sec
    n = max(1, int(load * 0.7))
    while erlang_a(calls, aht_sec, patience_sec, n, threshold_sec, interval_sec).service_level < target_sl:
        n += 1
    return n
