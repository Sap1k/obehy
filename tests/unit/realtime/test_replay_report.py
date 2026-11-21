"""The replay report reads the output stream (`replay_report.py`): `obehy rt replay` and `obehy rt
report --from-stream` on a compare stream give the same `report.json`, and the diagnostics
records change nothing else in the stream."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.canonical import line
from obehy.realtime.compare import compare
from obehy.realtime.policy import load_policy
from obehy.realtime.replay import ReplayOptions, named_channels, replay
from obehy.realtime.replay_report import from_stream
from tests.db.serving_fixture import write_release
from tests.realtime.builder import ORIGIN, STEP_LON, serving_rows, timetable

POLICY = load_policy()
DAY = date(2026, 10, 8)
START = datetime(2026, 10, 8, 5, 55, tzinfo=UTC)  # 07:55 CEST
DIAGNOSTIC = {"poll", "predictor_sample", "journey_sample"}


def _release(root: Path) -> Path:
    tt = timetable().stop("A", ORIGIN[0], ORIGIN[1])
    tt.stop("B", ORIGIN[0] + STEP_LON, ORIGIN[1]).stop("C", ORIGIN[0] + 2 * STEP_LON, ORIGIN[1])
    tt.trip("582492:143", days=[DAY], calls=[("A", "08:00"), ("B", "08:10"), ("C", "08:20")])
    return write_release(root, "run-a", jdf=serving_rows(tt))


def _archive(root: Path) -> None:
    """A bus on 582492:143 polled every 30 s for half an hour, one unknown key, one failed
    poll."""

    writer = ArchiveWriter(root)
    for n in range(60):
        at = START + timedelta(seconds=30 * n)
        minutes = (at - START).total_seconds() / 60 - 5
        metres = max(0.0, min(2000.0, minutes * 100))
        vehicles: list[dict[str, Any]] = [
            {
                "ID": 810,
                "Delay": 1,
                "RouteID": 143,
                "CISLineID": 582492,
                "Longitude": ORIGIN[0] + STEP_LON * metres / 1000,
                "Latitude": ORIGIN[1],
                "GPSPositionDT": (at - timedelta(seconds=3)).isoformat(),
                "Azimut": 90.0,
                "State": 2 if minutes < 0 else 0,
            },
            {
                "ID": 811,
                "RouteID": 7,
                "CISLineID": 999999,
                "Longitude": 14.5,
                "Latitude": 50.5,
                "GPSPositionDT": at.isoformat(),
                "State": 0,
            },
        ]
        body = json.dumps({"VehicleList": vehicles}).encode()
        if n == 7:
            writer.append("duk", "vehicles", Poll(at, at, None, None, error="timeout"), None)
            continue
        writer.append("duk", "vehicles", Poll(at, at, 200, body, "application/json"), body)


def _options(tmp_path: Path, **extra: Any) -> ReplayOptions:
    return ReplayOptions(
        release=tmp_path / "run-a",
        archive=tmp_path / "raw",
        start=DAY,
        end=DAY,
        channels=named_channels([("duk", "vehicles")]),
        out=tmp_path / "replay",
        feeds=("jdf", "czptt"),
        gtfs_rt_every_s=60,
        **extra,
    )


def _records(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(text) for text in stream]


def test_the_report_from_a_stream_file_is_the_replays(tmp_path: Path) -> None:
    _release(tmp_path)
    _archive(tmp_path / "raw")
    options = _options(tmp_path, quality=True)
    document = replay(None, options, POLICY)
    stream = tmp_path / "stream.jsonl.gz"
    compare(replace(options, diagnostics=True), "output", POLICY, stream)
    again = from_stream(stream, tmp_path / "again", POLICY.keyless.episode_gap_s)

    assert again == document
    assert (tmp_path / "again" / "report.json").read_bytes() == (
        tmp_path / "replay" / "report.json"
    ).read_bytes()
    channel = document["archive"]["channels"]["duk/vehicles"]
    assert (channel["polls"], channel["failed_polls"], channel["observations"]) == (60, 1, 118)
    assert document["running_key_groups"] == {"duk/duk": {"bound": 1, "key_groups": 2}}
    assert document["predictors"]["sz"] == {"n": 0}
    quality = document["quality"]
    assert quality["accuracy"]["bus"]["1"]["n"] > 0
    assert quality["checks"]["events"]["bus"]["departures"] >= 1


def test_the_diagnostics_records_change_nothing_else_in_the_stream(tmp_path: Path) -> None:
    _release(tmp_path)
    _archive(tmp_path / "raw")
    plain, diagnosed = tmp_path / "plain.jsonl.gz", tmp_path / "diagnosed.jsonl.gz"
    compare(_options(tmp_path), "output", POLICY, plain)
    compare(_options(tmp_path, quality=True), "output", POLICY, diagnosed)
    records = _records(diagnosed)
    header = records[0]
    assert header.pop("diagnostics") is True and header.pop("quality") is True
    assert [r for r in records if r["kind"] not in DIAGNOSTIC] == _records(plain)
    journeys = [j for r in records if r["kind"] == "journey_sample" for j in r["journeys"]]
    first, later = journeys[0], journeys[1:]
    assert first["plan"] is not None and len(first["calls"]) == first["n"] == 3
    # A journey's later samples carry only the calls that changed, and no plan.
    assert later and all(j["journey"] == first["journey"] for j in later)
    assert all(j["plan"] is None for j in later)
    assert any(len(j["calls"]) < j["n"] for j in later)


def test_journey_samples_write_their_floats_exactly() -> None:
    record = {"kind": "journey_sample", "lateness_s": 12.3456789, "along_m": 1e-05, "x": 90.0}
    assert line(record) == (
        '{"along_m":1e-05,"kind":"journey_sample","lateness_s":12.3456789,"x":90.0}\n'
    )
    assert line(record | {"kind": "other"}) == (
        '{"along_m":0,"kind":"other","lateness_s":12.346,"x":90}\n'
    )
