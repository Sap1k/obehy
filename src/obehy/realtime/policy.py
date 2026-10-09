"""Versioned realtime policy (`data/realtime/policy-v1.toml`); a missing value is a load error."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

POLICY = Path(__file__).resolve().parents[1] / "data" / "realtime" / "policy-v1.toml"


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
    max_clock_skew: timedelta


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
class Policy:
    version: str
    time: TimePolicy
    lifecycle: LifecyclePolicy
    progress: ProgressPolicy
    prediction: PredictionPolicy
    delay_discard_below_s: int
    warm_replay_hours: int
    emit_tick_s: int


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


def parse_policy(document: dict[str, Any]) -> Policy:
    root = _Table(document, "")
    time = root.table("time")
    lifecycle = root.table("lifecycle")
    progress = root.table("progress")
    prediction = root.table("prediction")
    return Policy(
        version=root.text("policy_version"),
        time=TimePolicy(
            pre_trip_s=time.by_mode("pre_trip_s"),
            max_delay_s=time.by_mode("max_delay_s"),
            vehicle_day_gap_s=time.integer("vehicle_day_gap_s"),
            max_clock_skew=timedelta(seconds=time.integer("max_clock_skew_s")),
        ),
        lifecycle=LifecyclePolicy(
            stale_after_s=lifecycle.by_mode("stale_after_s"),
            predict_without_data_s=lifecycle.by_mode("predict_without_data_s"),
            arrival_radius_m=lifecycle.number("arrival_radius_m"),
            departure_margin_m=lifecycle.number("departure_margin_m"),
            finished_grace_s=lifecycle.integer("finished_grace_s"),
            forget_after_s=lifecycle.integer("forget_after_s"),
        ),
        progress=ProgressPolicy(
            gps_sigma_m=progress.number("gps_sigma_m"),
            shape_sigma_m=progress.number("shape_sigma_m"),
            chord_min_sigma_m=progress.number("chord_min_sigma_m"),
            chord_k=progress.number("chord_k"),
            chord_max_sigma_m=progress.number("chord_max_sigma_m"),
            reach_sigmas=progress.number("reach_sigmas"),
            bearing_sigma_deg=progress.number("bearing_sigma_deg"),
            chord_bearing_sigma_deg=progress.number("chord_bearing_sigma_deg"),
            jitter_m=progress.number("jitter_m"),
            off_path_log_p=progress.number("off_path_log_p"),
            max_speed_mps=progress.by_mode("max_speed_mps"),
            loss_sigma_base_s=progress.number("loss_sigma_base_s"),
            loss_sigma_rate=progress.number("loss_sigma_rate"),
            gain_sigma_base_s=progress.number("gain_sigma_base_s"),
            gain_sigma_rate=progress.number("gain_sigma_rate"),
            start_lateness_sigma_s=progress.number("start_lateness_sigma_s"),
            beam=progress.integer("beam"),
            prune=progress.number("prune"),
            agree_within=progress.number("agree_within"),
            max_commit_lag_s=progress.integer("max_commit_lag_s"),
            max_event_interval_s=progress.integer("max_event_interval_s"),
            off_route_hold_s=progress.integer("off_route_hold_s"),
        ),
        prediction=PredictionPolicy(
            min_dwell_s=prediction.by_mode("min_dwell_s"),
            long_dwell_s=prediction.by_mode("long_dwell_s"),
            min_long_dwell_s=prediction.by_mode("min_long_dwell_s"),
        ),
        delay_discard_below_s=root.table("delay").integer("discard_below_s"),
        warm_replay_hours=root.table("warm_replay").integer("hours"),
        emit_tick_s=root.table("emit").integer("tick_s"),
    )


def load_policy(path: Path = POLICY) -> Policy:
    with path.open("rb") as stream:
        return parse_policy(tomllib.load(stream))
