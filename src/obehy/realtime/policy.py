"""Versioned realtime policy (`data/realtime/policy-v2.toml`); a missing value is a load error."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from datetime import timedelta
from pathlib import Path
from typing import Any, cast, get_type_hints

POLICY = Path(__file__).resolve().parents[1] / "data" / "realtime" / "policy-v2.toml"


class PolicyError(ValueError):
    """The policy file is missing a value or has one of the wrong type."""


@dataclass(frozen=True, slots=True)
class ByMode:
    """A value with per-route-mode overrides."""

    default: float
    overrides: tuple[tuple[str, float], ...]

    def __call__(self, mode: str) -> float:
        for name, value in self.overrides:
            if name == mode:
                return value
        return self.default


@dataclass(frozen=True, slots=True)
class TimePolicy:
    pre_trip_s: ByMode
    max_delay_s: ByMode
    vehicle_day_gap_s: int
    max_clock_skew_s: int

    @property
    def max_clock_skew(self) -> timedelta:
        return timedelta(seconds=self.max_clock_skew_s)


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    stale_after_s: ByMode
    predict_without_data_s: ByMode
    arrival_radius_m: float
    departure_margin_m: float
    finished_grace_s: int
    forget_after_s: int


@dataclass(frozen=True, slots=True)
class ProgressPolicy:
    gps_sigma_m: float
    shape_sigma_m: float
    chord_min_sigma_m: float
    chord_k: float
    chord_max_sigma_m: float
    reach_sigmas: float
    bearing_sigma_deg: float
    chord_bearing_sigma_deg: float
    jitter_m: float
    off_path_log_p: float
    max_speed_mps: ByMode
    loss_sigma_base_s: float
    loss_sigma_rate: float
    gain_sigma_base_s: float
    gain_sigma_rate: float
    start_lateness_sigma_s: float
    beam: int
    prune: float
    agree_within: float
    max_commit_lag_s: int
    max_event_interval_s: int
    off_route_hold_s: int


@dataclass(frozen=True, slots=True)
class PredictionPolicy:
    min_dwell_s: ByMode
    long_dwell_s: ByMode
    min_long_dwell_s: ByMode


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Proxy pools of egress channels (docs/R2_SLICE.md section 7)."""

    refresh_s: int  # how often the proxy list is downloaded again
    cooldown_s: int  # how long a proxy that failed or was refused stays out of rotation


@dataclass(frozen=True, slots=True)
class Policy:
    version: str
    time: TimePolicy
    lifecycle: LifecyclePolicy
    progress: ProgressPolicy
    prediction: PredictionPolicy
    delay_discard_below_s: int
    warm_replay_hours: int
    emit_tick_s: int
    observation_retention_days: int
    egress: EgressPolicy


class _Table:
    def __init__(self, data: dict[str, Any], path: str) -> None:
        self.data = data
        self.path = path

    def _get(self, key: str) -> object:
        if key not in self.data:
            raise PolicyError(f"missing {self.path}{key}")
        return self.data[key]

    def table(self, key: str) -> _Table:
        value = self._get(key)
        if not isinstance(value, dict):
            raise PolicyError(f"{self.path}{key} must be a table")
        return _Table(cast(dict[str, Any], value), f"{self.path}{key}.")

    def number(self, key: str) -> float:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise PolicyError(f"{self.path}{key} must be a number")
        return float(value)

    def integer(self, key: str) -> int:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise PolicyError(f"{self.path}{key} must be an integer")
        return value

    def text(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str) or not value:
            raise PolicyError(f"{self.path}{key} must be a non-empty string")
        return value

    def by_mode(self, key: str) -> ByMode:
        table = self.table(key)
        default = table.number("default")
        overrides = tuple(
            sorted((name, table.number(name)) for name in table.data if name != "default")
        )
        return ByMode(default, overrides)

    def section[T](self, key: str, cls: type[T]) -> T:
        """A policy section: every field of `cls` read by name and type; no key may be missing
        and none may be unknown, so a typo in the file is an error, not a silent default."""

        table = self.table(key)
        hints = get_type_hints(cls)
        names = [f.name for f in fields(cast(Any, cls))]
        unknown = sorted(set(table.data) - set(names))
        if unknown:
            raise PolicyError(f"unknown {table.path}{unknown[0]}")
        readers = {"float": table.number, "int": table.integer, "ByMode": table.by_mode}
        values = {name: readers[hints[name].__name__](name) for name in names}
        return cls(**values)


def parse_policy(document: dict[str, Any]) -> Policy:
    root = _Table(document, "")
    return Policy(
        version=root.text("policy_version"),
        time=root.section("time", TimePolicy),
        lifecycle=root.section("lifecycle", LifecyclePolicy),
        progress=root.section("progress", ProgressPolicy),
        prediction=root.section("prediction", PredictionPolicy),
        delay_discard_below_s=root.table("delay").integer("discard_below_s"),
        warm_replay_hours=root.table("warm_replay").integer("hours"),
        emit_tick_s=root.table("emit").integer("tick_s"),
        observation_retention_days=root.table("retention").integer("observation_days"),
        egress=root.section("egress", EgressPolicy),
    )


def load_policy(path: Path = POLICY) -> Policy:
    with path.open("rb") as stream:
        return parse_policy(tomllib.load(stream))
