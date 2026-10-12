"""Replay diagnostics for keyless sources (`infer/keyless`): coverage per episode.

A group is one vehicle's run of observations with the same line and destination, split after
`episode_gap_s` without one, as the inference's episodes are. `report.json` gets the counts;
`keyless.jsonl` one row per group (journeys bound, reasons, departures seen), for checking the
bindings against the timetable by hand.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from obehy.realtime.infer.keyless.episode import same_name
from obehy.realtime.model import (
    Destination,
    Effect,
    LineRef,
    NextStopName,
    ObservationResult,
    TripKey,
    VehicleKey,
)
from obehy.realtime.times import Instant


@dataclass(slots=True)
class _Group:
    source: str
    vehicle: str
    line: str
    destination: str
    first_at: Instant
    last_at: Instant
    next_stop: str | None = None
    observations: int = 0
    bound: int = 0
    departures: int = 0
    first_bound_at: Instant | None = None
    journeys: list[str] = field(default_factory=list[str])
    reasons: Counter[str] = field(default_factory=Counter[str])

    def row(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "vehicle": self.vehicle,
            "line": self.line,
            "destination": self.destination,
            "from": self.first_at.isoformat(),
            "to": self.last_at.isoformat(),
            "observations": self.observations,
            "bound": self.bound,
            "departures": self.departures,
            "minutes_to_bind": _minutes(self.first_at, self.first_bound_at),
            "journeys": self.journeys,
            "reasons": dict(sorted(self.reasons.items())),
        }


def _minutes(start: Instant, end: Instant | None) -> float | None:
    return None if end is None else round((end - start).total_seconds() / 60, 1)


class KeylessLog:
    def __init__(self, episode_gap_s: int) -> None:
        self.gap = timedelta(seconds=episode_gap_s)
        self.groups: list[_Group] = []
        self.current: dict[tuple[str, str], _Group] = {}

    def observe(self, effects: Sequence[Effect]) -> None:
        for effect in effects:
            if not isinstance(effect, ObservationResult):
                continue
            obs = effect.observation
            vehicle, line = obs.first(VehicleKey), obs.first(LineRef)
            destination = obs.first(Destination)
            if obs.first(TripKey) is not None or vehicle is None or line is None:
                continue
            if destination is None:
                continue
            key = (obs.source, vehicle.source_vehicle_id)
            group = self.current.get(key)
            if (
                group is None
                or group.line != line.value
                or not same_name(group.destination, destination.name)
                or obs.at - group.last_at > self.gap
            ):
                group = _Group(
                    obs.source,
                    vehicle.source_vehicle_id,
                    line.value,
                    destination.name,
                    obs.at,
                    obs.at,
                )
                self.groups.append(group)
                self.current[key] = group
            group.last_at = max(group.last_at, obs.at)
            group.observations += 1
            following = obs.first(NextStopName)
            if following is not None:
                if group.next_stop is not None and not same_name(following.name, group.next_stop):
                    group.departures += 1
                group.next_stop = following.name
            if effect.journey is None:
                group.reasons[str(effect.reason)] += 1
                continue
            group.bound += 1
            if group.first_bound_at is None:
                group.first_bound_at = obs.at
            name = f"{effect.journey.key}@{effect.journey.service_date.isoformat()}"
            if name not in group.journeys:
                group.journeys.append(name)

    def report(self) -> dict[str, Any]:
        by_source: dict[str, Counter[str]] = {}
        waits: dict[str, list[float]] = {}
        for group in self.groups:
            counter = by_source.setdefault(group.source, Counter())
            counter["groups"] += 1
            counter["with_departure"] += int(group.departures > 0)
            counter["bound"] += int(group.bound > 0)
            counter["bound_with_departure"] += int(group.bound > 0 and group.departures > 0)
            counter["several_journeys"] += int(len(group.journeys) > 1)
            counter["observations"] += group.observations
            counter["bound_observations"] += group.bound
            if not group.bound and group.reasons:
                counter[f"never_bound/{group.reasons.most_common(1)[0][0]}"] += 1
            wait = _minutes(group.first_at, group.first_bound_at)
            if wait is not None:
                waits.setdefault(group.source, []).append(wait)
        out: dict[str, Any] = {}
        for source, counter in sorted(by_source.items()):
            entry: dict[str, Any] = dict(sorted(counter.items()))
            if source in waits:
                entry["median_minutes_to_bind"] = statistics.median(waits[source])
            out[source] = entry
        return out

    def write(self, path: Path) -> None:
        rows = sorted(
            (group.row() for group in self.groups),
            key=lambda row: (row["source"], row["vehicle"], row["from"]),
        )
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
