"""The API's versioned limits (`src/obehy/data/api/api-v1.toml`)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

LIMITS = Path(__file__).resolve().parents[1] / "data" / "api" / "api-v1.toml"


@dataclass(frozen=True, slots=True)
class Limits:
    version: str
    search_min_chars: int
    search_limit: int
    search_limit_max: int
    stops_bbox_max_deg2: float
    stops_bbox_limit: int
    near_radius_m: int
    near_radius_max_m: int
    near_limit: int
    lookback_min: int
    departures_minutes: int
    departures_minutes_max: int
    departures_limit: int
    departures_limit_max: int
    board_stops_max: int
    lines_search_limit: int
    lines_search_limit_max: int
    line_patterns: int
    history_max_days: int
    history_default_days: int
    on_time_early_s: int
    on_time_late_s: int
    alerts_limit: int
    composition_fresh_s: int
    composition_retry_after_s: int
    debug_window_min: int
    debug_limit: int
    vehicle_max_age_s: int
    vehicles_bbox_max_deg2: float
    vehicles_limit: int
    vehicles_search_limit: int
    vehicles_search_limit_max: int
    cache_realtime_s: int
    cache_static_s: int


def load_limits(path: Path = LIMITS) -> Limits:
    with path.open("rb") as stream:
        document = tomllib.load(stream)

    def table(name: str) -> dict[str, Any]:
        return cast(dict[str, Any], document[name])

    stops, departures, vehicles, cache = (
        table("stops"),
        table("departures"),
        table("vehicles"),
        table("cache"),
    )
    lines, history, debug = table("lines"), table("history"), table("debug")
    alerts, compositions = table("alerts"), table("compositions")
    return Limits(
        version=str(document["api_version"]),
        search_min_chars=int(stops["search_min_chars"]),
        search_limit=int(stops["search_limit"]),
        search_limit_max=int(stops["search_limit_max"]),
        stops_bbox_max_deg2=float(stops["bbox_max_deg2"]),
        stops_bbox_limit=int(stops["bbox_limit"]),
        near_radius_m=int(stops["near_radius_m"]),
        near_radius_max_m=int(stops["near_radius_max_m"]),
        near_limit=int(stops["near_limit"]),
        lookback_min=int(departures["lookback_min"]),
        departures_minutes=int(departures["minutes"]),
        departures_minutes_max=int(departures["minutes_max"]),
        departures_limit=int(departures["limit"]),
        departures_limit_max=int(departures["limit_max"]),
        board_stops_max=int(departures["stops_max"]),
        lines_search_limit=int(lines["search_limit"]),
        lines_search_limit_max=int(lines["search_limit_max"]),
        line_patterns=int(lines["patterns"]),
        history_max_days=int(history["max_days"]),
        history_default_days=int(history["default_days"]),
        on_time_early_s=int(history["on_time_early_s"]),
        on_time_late_s=int(history["on_time_late_s"]),
        alerts_limit=int(alerts["limit"]),
        composition_fresh_s=int(compositions["fresh_s"]),
        composition_retry_after_s=int(compositions["retry_after_s"]),
        debug_window_min=int(debug["window_min"]),
        debug_limit=int(debug["limit"]),
        vehicle_max_age_s=int(vehicles["max_age_s"]),
        vehicles_bbox_max_deg2=float(vehicles["bbox_max_deg2"]),
        vehicles_limit=int(vehicles["limit"]),
        vehicles_search_limit=int(vehicles["search_limit"]),
        vehicles_search_limit_max=int(vehicles["search_limit_max"]),
        cache_realtime_s=int(cache["realtime_s"]),
        cache_static_s=int(cache["static_s"]),
    )
