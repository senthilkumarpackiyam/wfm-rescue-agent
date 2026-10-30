"""Typed loaders for config/*.yaml. Every component reads the centre's shape from here."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


class ServiceLevel(BaseModel):
    target_pct: float
    threshold_seconds: int


class QueueConfig(BaseModel):
    label: str
    weekday_daily_volume: float
    aht_seconds: float
    aht_cv: float
    mean_patience_seconds: float
    reason_mix: dict[str, float]

    @model_validator(mode="after")
    def _mix_sums_to_one(self) -> "QueueConfig":
        total = sum(self.reason_mix.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"reason_mix for {self.label} sums to {total}, expected 1.0")
        return self


class DemandModelConfig(BaseModel):
    day_of_week: list[float] = Field(min_length=7, max_length=7)
    annual_growth_pct: float
    annual_seasonality_pct: float
    daily_noise_cv: float
    aht_daily_noise_cv: float
    intraday_bumps: list[tuple[float, float, float]]


class StaffingConfig(BaseModel):
    shrinkage_pct: float
    normal_absence_pct: float
    planning_sl_target_pct: float
    cross_skilled_pct: float
    agent_hourly_cost_inr: float
    overtime_multiplier: float
    full_time_shift_minutes: int
    part_time_shift_minutes: int
    coaching_share: float
    compliance_share: float
    roster_seed: int


class HistoryConfig(BaseModel):
    start_date: str
    end_date: str
    seed: int


class CentreConfig(BaseModel):
    name: str
    currency: str
    open_time: str
    close_time: str
    interval_minutes: int
    service_level: ServiceLevel
    queues: dict[str, QueueConfig]
    demand_model: DemandModelConfig
    staffing: StaffingConfig
    history: HistoryConfig

    @property
    def queue_ids(self) -> list[str]:
        return list(self.queues)


def load_env() -> None:
    """Copy KEY=value lines from the repo's .env into the environment.
    A variable already set in the shell wins over the file."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value and not os.environ.get(key):
            os.environ[key] = value.strip("'\"")


def _read_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


@lru_cache
def load_centre() -> CentreConfig:
    return CentreConfig.model_validate(_read_yaml("contact_center.yaml"))


@lru_cache
def load_levers() -> dict[str, Any]:
    """Lever catalogue as plain dicts; Phase 4 wraps these in typed lever classes."""
    return _read_yaml("levers.yaml")
