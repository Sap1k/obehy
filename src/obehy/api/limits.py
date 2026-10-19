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
    lookback_min: int
    departures_minutes: int
    departures_minutes_max: int
    departures_limit: int
    departures_limit_max: int
    vehicle_max_age_s: int
    vehicles_bbox_max_deg2: float
    vehicles_limit: int
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
    return Limits(
        version=str(document["api_version"]),
        search_min_chars=int(stops["search_min_chars"]),
        search_limit=int(stops["search_limit"]),
        search_limit_max=int(stops["search_limit_max"]),
        stops_bbox_max_deg2=float(stops["bbox_max_deg2"]),
        stops_bbox_limit=int(stops["bbox_limit"]),
        lookback_min=int(departures["lookback_min"]),
        departures_minutes=int(departures["minutes"]),
        departures_minutes_max=int(departures["minutes_max"]),
        departures_limit=int(departures["limit"]),
        departures_limit_max=int(departures["limit_max"]),
        vehicle_max_age_s=int(vehicles["max_age_s"]),
        vehicles_bbox_max_deg2=float(vehicles["bbox_max_deg2"]),
        vehicles_limit=int(vehicles["limit"]),
        cache_realtime_s=int(cache["realtime_s"]),
        cache_static_s=int(cache["static_s"]),
    )
