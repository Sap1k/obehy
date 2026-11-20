"""Golden outputs of the real-trace corpus (`tests/realtime/corpus.py`): what the core makes of each
journey, as reviewable JSON in `tests/golden/corpus/<name>.json`.

Per journey:
- `effects`: everything the core asked to be written except the schedule snapshots (static
  data): history stop events, vehicle assignments, cancellations, journey links, platforms;
- `timeline`: for each journey instance, every tick at which its lifecycle, its journey delay or
  the number of calls it has passed changed;
- `final`: each instance's last state (lifecycle, delay, each source's delay, every call's
  status, source class, passed and estimated times) and each vehicle's last state.

This is the behaviour a change of the core is judged against, and what a port of the core must
reproduce. After a deliberate change, rewrite the goldens and review their diff:

    uv run python -m tests.realtime.golden diff     # breakdown of a fresh run against them
    uv run python -m tests.realtime.golden write    # rewrite them
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from obehy.realtime.model import (
    Effect,
    FeedState,
    Instance,
    JourneyKey,
    ObservationResult,
    SnapshotJourney,
    Unresolved,
    WriteEvent,
)
from obehy.realtime.times import Instant
from tests.realtime.corpus import names, run

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "corpus"


def _json(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__name__} | {
            f.name: _json(getattr(value, f.name)) for f in fields(value)
        }
    if isinstance(value, dict):
        return {str(_json(k)): _json(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple | set | frozenset):
        return [_json(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return str(value)


def journey_name(key: JourneyKey) -> str:
    return f"{key.feed}/{key.namespace}/{key.key}/{key.service_date.isoformat()}"


def _effect(effect: Effect) -> list[Any]:
    """One effect as a compact row led by its type."""

    if isinstance(effect, WriteEvent):
        return [
            "event",
            journey_name(effect.journey),
            effect.location_id,
            effect.visit_n,
            effect.kind,
            effect.interval.lo.isoformat(),
            effect.interval.hi.isoformat(),
            effect.method,
            effect.source,
        ]
    if isinstance(effect, ObservationResult):
        o = effect.observation
        return [
            "observed",
            o.received_at.isoformat(),
            o.source,
            o.raw.item,
            None if effect.journey is None else journey_name(effect.journey),
            _json(effect.reason),
            [journey_name(j) for j in effect.public],
            _json(effect.state_since),
        ]
    if isinstance(effect, Unresolved):
        o = effect.observation
        return ["unresolved", o.received_at.isoformat(), o.source, effect.kind, effect.name]
    document = _json(effect)
    return [document.pop("type"), document]


def _passed(instance: Instance) -> int:
    return sum(
        1 for c in instance.calls if c.passed_arrival is not None or c.passed_departure is not None
    )


def _final(instance: Instance) -> dict[str, Any]:
    return {
        "lifecycle": instance.lifecycle,
        "delay_s": instance.lateness_s,
        "reported_delay_s": instance.reported_delay_s,
        "off_route": instance.off_route,
        "sources": {s.source: s.delay_s for s in instance.sources},
        "calls": [
            {
                "sequence": c.sequence,
                "location_id": c.location_id,
                "visit_n": c.visit_n,
                "status": c.status,
                "source_class": _json(c.source_class),
                "passed": [_json(c.passed_arrival), _json(c.passed_departure)],
                "estimated": [_json(c.estimated_arrival), _json(c.estimated_departure)],
            }
            for c in instance.calls
        ],
    }


def golden(name: str) -> dict[str, Any]:
    """One journey's outputs from a fresh run of the core."""

    timeline: dict[str, list[list[Any]]] = {}
    last: dict[str, tuple[Any, ...]] = {}

    def tick(state: FeedState, now: Instant) -> None:
        for key, instance in sorted(state.instances.items(), key=lambda kv: journey_name(kv[0])):
            journey = journey_name(key)
            value = (instance.lifecycle, instance.lateness_s, _passed(instance))
            if last.get(journey) != value:
                last[journey] = value
                timeline.setdefault(journey, []).append([now.isoformat(), *value])

    result = run(name, tick=tick)
    state = result.state
    return {
        "effects": [_effect(e) for e in result.effects if not isinstance(e, SnapshotJourney)],
        "timeline": timeline,
        "final": {
            "instances": {
                journey_name(k): _final(i)
                for k, i in sorted(state.instances.items(), key=lambda kv: journey_name(kv[0]))
            },
            "vehicles": {
                str(v): {
                    "status": _json(s.status),
                    "reason": _json(s.reason),
                    "journey": None if s.binding is None else journey_name(s.binding.journey),
                    "method": None if s.binding is None else _json(s.binding.method),
                }
                for v, s in sorted(state.vehicles.items(), key=lambda kv: str(kv[0]))
            },
        },
    }


def path(name: str) -> Path:
    return GOLDEN / f"{name}.json"


def _line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _pretty(value: Any, depth: int = 0) -> str:
    """Objects one key a line, a list of rows or objects one element a line, rows inline."""

    pad, inner = " " * depth, " " * (depth + 1)
    if isinstance(value, dict) and value:
        entries: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        items = [f"{inner}{_line(k)}: {_pretty(v, depth + 1)}" for k, v in sorted(entries.items())]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list) and value:
        elements: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        if all(isinstance(v, list | dict) for v in elements):
            items = [
                inner + (_pretty(v, depth + 1) if isinstance(v, dict) else _line(v))
                for v in elements
            ]
            return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return _line(value)


def dump(document: dict[str, Any]) -> str:
    return _pretty(document) + "\n"


def write() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    wanted = set(names())
    for stale in GOLDEN.glob("*.json"):
        if stale.stem not in wanted:
            stale.unlink()
    for name in sorted(wanted):
        path(name).write_text(dump(golden(name)), encoding="utf-8", newline="\n")


# --- the breakdown of a change ------------------------------------------------------------------


def _seconds(a: str | None, b: str | None) -> float | None:
    if a is None or b is None:
        return None
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def _event_key(e: list[Any]) -> tuple[Any, ...]:
    return (e[1], e[2], e[3], e[4])


def _series(changes: list[list[Any]], at: list[str]) -> list[tuple[Any, ...]]:
    """The change log forward-filled onto the instants `at` (None before its first change)."""

    out: list[tuple[Any, ...]] = []
    i, current = 0, (None, None, None)
    for t in at:
        while i < len(changes) and changes[i][0] <= t:
            current = tuple(changes[i][1:])
            i += 1
        out.append(current)
    return out


class Breakdown:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.event_moves: list[float] = []
        self.delay_moves: list[float] = []
        self.examples: dict[str, list[str]] = {}

    def note(self, what: str, example: str) -> None:
        self.counts[what] += 1
        bucket = self.examples.setdefault(what, [])
        if len(bucket) < 5:
            bucket.append(example)

    def compare(self, name: str, old: dict[str, Any], new: dict[str, Any]) -> None:
        self.counts["journeys"] += 1
        if old == new:
            self.counts["journeys unchanged"] += 1
            return
        old_events = {_event_key(e): e for e in old["effects"] if e[0] == "event"}
        new_events = {_event_key(e): e for e in new["effects"] if e[0] == "event"}
        for key in old_events.keys() - new_events.keys():
            self.note("stop events lost", f"{name} {key}")
        for key in new_events.keys() - old_events.keys():
            self.note("stop events gained", f"{name} {key}")
        for key in old_events.keys() & new_events.keys():
            a, b = old_events[key], new_events[key]
            if a[5:7] != b[5:7]:
                move = _seconds(a[5], b[5]) or 0.0
                self.event_moves.append(move)
                self.note("stop events moved", f"{name} {key} {move:+.0f} s")
            if a[7] != b[7]:
                self.note("stop event method changed", f"{name} {key}")
        old_seen = {tuple(e[1:4]): e[4:6] for e in old["effects"] if e[0] == "observed"}
        new_seen = {tuple(e[1:4]): e[4:6] for e in new["effects"] if e[0] == "observed"}
        rebound = [k for k in old_seen.keys() & new_seen.keys() if old_seen[k] != new_seen[k]]
        if rebound:
            self.counts["observations bound differently"] += len(rebound)
            self.note("journeys with observations bound differently", name)
        other = {"event", "observed"}
        kinds = {e[0] for e in old["effects"] + new["effects"]} - other
        for kind in sorted(kinds):
            a = sorted(_line(e) for e in old["effects"] if e[0] == kind)
            b = sorted(_line(e) for e in new["effects"] if e[0] == kind)
            if a != b:
                self.note(f"{kind} effects changed", name)
        for journey in sorted(old["timeline"].keys() | new["timeline"].keys()):
            a_log = old["timeline"].get(journey, [])
            b_log = new["timeline"].get(journey, [])
            if a_log == b_log:
                continue
            at = sorted({c[0] for c in a_log} | {c[0] for c in b_log})
            a_series, b_series = _series(a_log, at), _series(b_log, at)
            if [s[0] for s in a_series] != [s[0] for s in b_series]:
                self.note("lifecycle timeline changed", f"{name} {journey}")
            if [s[2] for s in a_series] != [s[2] for s in b_series]:
                self.note("passed-calls timeline changed", f"{name} {journey}")
            delays = [(x[1], y[1]) for x, y in zip(a_series, b_series, strict=True) if x[1] != y[1]]
            if delays:
                self.note("journey delay timeline changed", f"{name} {journey}")
                self.delay_moves.extend(
                    float(y - x) for x, y in delays if x is not None and y is not None
                )
        old_final, new_final = old["final"], new["final"]
        if old_final["vehicles"] != new_final["vehicles"]:
            self.note("final vehicle state changed", name)
        for journey in sorted(old_final["instances"].keys() ^ new_final["instances"].keys()):
            self.note("instances appeared or vanished", f"{name} {journey}")
        for journey in sorted(old_final["instances"].keys() & new_final["instances"].keys()):
            a, b = old_final["instances"][journey], new_final["instances"][journey]
            if a["calls"] != b["calls"]:
                self.note("final call states changed", f"{name} {journey}")
            if a["sources"] != b["sources"]:
                self.note("final source delays changed", f"{name} {journey}")

    def report(self) -> dict[str, Any]:
        def spread(values: list[float]) -> dict[str, Any] | None:
            if not values:
                return None
            absolute = sorted(abs(v) for v in values)
            return {
                "n": len(values),
                "median_abs_s": statistics.median(absolute),
                "max_abs_s": absolute[-1],
                "mean_s": round(statistics.fmean(values), 1),
            }

        return {
            "counts": dict(sorted(self.counts.items())),
            "stop event moves": spread(self.event_moves),
            "journey delay changes": spread(self.delay_moves),
            "examples": self.examples,
        }


def diff() -> dict[str, Any]:
    breakdown = Breakdown()
    for name in names():
        stored = path(name)
        if not stored.is_file():
            breakdown.note("journeys without a golden", name)
            continue
        old = json.loads(stored.read_text(encoding="utf-8"))
        breakdown.compare(name, old, json.loads(dump(golden(name))))
    return breakdown.report()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["write", "diff"])
    parser.add_argument("--out", type=Path, help="write the diff's breakdown here as JSON")
    args = parser.parse_args()
    if args.command == "write":
        write()
        return
    report = diff()
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
