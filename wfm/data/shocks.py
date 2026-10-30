"""How a Shock bends the day over time (shared by the simulator and the charts)."""

from __future__ import annotations

import numpy as np

from wfm.schemas import Shock
from wfm.timegrid import hhmm_to_minutes


def shock_multiplier(shock: Shock, clock_minutes: np.ndarray | float) -> np.ndarray:
    """Multiplier vs a normal day at each wall-clock minute (minutes since midnight).

    volume / aht: 1 -> magnitude over ramp_min, hold, then back to 1 over decay_min.
    absence:      share of capacity still available (1 before start, 1 - magnitude after).
    """
    t = np.asarray(clock_minutes, dtype=float)
    start = hhmm_to_minutes(shock.start)
    if shock.kind == "absence":
        return np.where(t >= start, 1.0 - shock.magnitude, 1.0)

    elapsed = t - start
    ramp = np.clip(elapsed / shock.ramp_min, 0, 1) if shock.ramp_min else (elapsed >= 0).astype(float)
    shape = np.where(elapsed >= 0, ramp, 0.0)
    if shock.duration_min is not None:
        after = elapsed - shock.duration_min
        fade = 1 - np.clip(after / shock.decay_min, 0, 1) if shock.decay_min else (after < 0).astype(float)
        shape = np.where(after > 0, fade, shape)
    return 1.0 + (shock.magnitude - 1.0) * shape
