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
    stale_after_s: int
    off_route_base_m: ByMode
    off_route_k: float
    off_route_hold_s: int
    max_speed_mps: ByMode
    backtrack_tolerance_m: float
    arrival_radius_m: float
    departure_margin_m: float
    finished_grace_s: int
    forget_after_s: int


@dataclass(frozen=True, slots=True)
class Policy:
    version: str
    time: TimePolicy
    lifecycle: LifecyclePolicy
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
    return Policy(
        version=root.text("policy_version"),
        time=TimePolicy(
            pre_trip_s=time.by_mode("pre_trip_s"),
            max_delay_s=time.by_mode("max_delay_s"),
            vehicle_day_gap_s=time.integer("vehicle_day_gap_s"),
            max_clock_skew=timedelta(seconds=time.integer("max_clock_skew_s")),
        ),
        lifecycle=LifecyclePolicy(
            stale_after_s=lifecycle.integer("stale_after_s"),
            off_route_base_m=lifecycle.by_mode("off_route_base_m"),
            off_route_k=lifecycle.number("off_route_k"),
            off_route_hold_s=lifecycle.integer("off_route_hold_s"),
            max_speed_mps=lifecycle.by_mode("max_speed_mps"),
            backtrack_tolerance_m=lifecycle.number("backtrack_tolerance_m"),
            arrival_radius_m=lifecycle.number("arrival_radius_m"),
            departure_margin_m=lifecycle.number("departure_margin_m"),
            finished_grace_s=lifecycle.integer("finished_grace_s"),
            forget_after_s=lifecycle.integer("forget_after_s"),
        ),
        delay_discard_below_s=root.table("delay").integer("discard_below_s"),
        warm_replay_hours=root.table("warm_replay").integer("hours"),
        emit_tick_s=root.table("emit").integer("tick_s"),
    )


def load_policy(path: Path = POLICY) -> Policy:
    with path.open("rb") as stream:
        return parse_policy(tomllib.load(stream))
