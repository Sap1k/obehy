from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from obehy import cli
from obehy.realtime import decode, replay
from obehy.realtime.archive import ArchiveWriter, Poll, day_directory
from obehy.realtime.episodes import split_episodes

DAY = date(2026, 10, 6)
ALL_DAYS = {"valid_from": date(2026, 10, 1), "valid_to": date(2026, 12, 12), "weekday_mask": 127}


def _hms(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def _package(root: Path, tables: dict[str, list[dict[str, Any]]]) -> None:
    serving = root / "serving"
    serving.mkdir(parents=True)
    relations: list[dict[str, Any]] = []
    schemas = {
        "source_key": [
            ("namespace", pa.string()),
            ("identifier", pa.string()),
            ("public_id", pa.string()),
            ("valid_from", pa.date32()),
            ("valid_to", pa.date32()),
        ],
        "trip": [("trip_id", pa.string()), ("service_id", pa.string()), ("run_key", pa.string())],
        "service_calendar": [
            ("service_id", pa.string()),
            ("valid_from", pa.date32()),
            ("valid_to", pa.date32()),
            ("weekday_mask", pa.int16()),
        ],
        "service_exception": [
            ("service_id", pa.string()),
            ("service_date", pa.date32()),
            ("added", pa.bool_()),
        ],
        "trip_call": [
            ("trip_id", pa.string()),
            ("sequence", pa.int32()),
            ("location_id", pa.string()),
            ("scheduled_arrival", pa.int32()),
            ("scheduled_departure", pa.int32()),
        ],
        "location": [
            ("location_id", pa.string()),
            ("parent_location_id", pa.string()),
            ("name", pa.string()),
        ],
    }
    for name, columns in schemas.items():
        rows = tables.get(name, [])
        pq.write_table(  # pyright: ignore[reportUnknownMemberType]
            pa.Table.from_pylist(rows, schema=pa.schema(columns)), serving / f"{name}.parquet"
        )
        relations.append({"name": name, "path": f"serving/{name}.parquet", "row_count": len(rows)})
    manifest = {
        "bundle_format": "jrutil-production",
        "bundle_version": 3,
        "serving_schema_version": "5.0",
        "contract_valid": True,
        "feed_version": f"sha256:{root.name}",
        "relations": relations,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "gtfs.zip").write_bytes(b"")
    (root / "diagnostics.json").write_text("{}", encoding="utf-8")


def _calls(trip_id: str, stops: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "trip_id": trip_id,
            "sequence": number,
            "location_id": location,
            "scheduled_arrival": _hms(at),
            "scheduled_departure": _hms(at),
        }
        for number, (location, at) in enumerate(stops, start=1)
    ]


def _key(namespace: str, identifier: str, public_id: str) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "identifier": identifier,
        "public_id": public_id,
        "valid_from": date(2026, 10, 1),
        "valid_to": date(2026, 12, 12),
    }


def _release(root: Path) -> Path:
    release = root / "release"
    _package(
        release / "jdf",
        {
            "source_key": [
                _key("cis:line", "582492", "jdf:route:582492"),
                _key("cis:line_trip", "582492:143", "t-day"),
                _key("cis:line_trip", "582492:150", "t-sunday"),
                _key("cis:line_trip", "582492:900", "t-night"),
                _key("cis:line_trip", "157710:1", "t-litvinov-noon"),
                _key("cis:line_trip", "157710:3", "t-litvinov-later"),
                _key("cis:line_trip", "157710:2", "t-praha"),
                _key("pid:gtfs_trip_id", "ignored", "t-day"),
            ],
            "trip": [
                {"trip_id": trip, "service_id": service, "run_key": None}
                for trip, service in [
                    ("t-day", "all"),
                    ("t-sunday", "sunday"),
                    ("t-night", "monday-only"),
                    ("t-litvinov-noon", "all"),
                    ("t-litvinov-later", "all"),
                    ("t-praha", "all"),
                ]
            ],
            "service_calendar": [
                {"service_id": "all", **ALL_DAYS},
                {"service_id": "sunday", **ALL_DAYS, "weekday_mask": 64},
            ],
            "service_exception": [
                {"service_id": "monday-only", "service_date": date(2026, 10, 5), "added": True},
            ],
            "trip_call": [
                *_calls("t-day", [("a", "07:00"), ("b", "08:00")]),
                *_calls("t-sunday", [("a", "07:00"), ("b", "08:00")]),
                *_calls("t-night", [("a", "24:30"), ("b", "25:00")]),
                *_calls(
                    "t-litvinov-noon", [("praha", "10:00"), ("most", "12:00"), ("lit", "13:00")]
                ),
                *_calls(
                    "t-litvinov-later", [("praha", "11:00"), ("most", "13:00"), ("lit", "14:00")]
                ),
                *_calls("t-praha", [("lit", "10:00"), ("most", "11:00"), ("praha", "13:00")]),
            ],
            "location": [
                {"location_id": "praha", "parent_location_id": None, "name": "Praha,,Florenc"},
                {"location_id": "most-place", "parent_location_id": None, "name": "Most,,nádraží"},
                {"location_id": "most", "parent_location_id": "most-place", "name": "Most 1"},
                {"location_id": "lit", "parent_location_id": None, "name": "Litvínov,, nádraží"},
            ],
        },
    )
    _package(
        release / "czptt",
        {
            "source_key": [
                _key("czptt:tr", "Tr:3189:KASO---57501:00:2026", "c-16001"),
                _key("czptt:train_number", "16001", "c-16001"),
                _key("czptt:tr", "Tr:3246:AMBIG:00:2026", "c-1011"),
                _key("czptt:tr", "Tr:3246:AMBIG:00:2026", "c-1013"),
                _key("czptt:train_number", "1011", "c-1011"),
                _key("czptt:train_number", "1013", "c-1013"),
            ],
            "trip": [
                {"trip_id": "c-16001", "service_id": "all", "run_key": "run-16001"},
                {"trip_id": "c-1011", "service_id": "all", "run_key": "run-1011"},
                {"trip_id": "c-1013", "service_id": "all", "run_key": "run-1013"},
            ],
            "service_calendar": [{"service_id": "all", **ALL_DAYS}],
            "trip_call": [
                *_calls("c-16001", [("x", "06:00"), ("y", "09:00")]),
                *_calls("c-1011", [("x", "06:00"), ("y", "09:00")]),
                *_calls("c-1013", [("x", "10:00"), ("y", "12:00")]),
            ],
        },
    )
    (release / "release.json").write_text(
        json.dumps({"schema_version": 1, "run_id": "20261006T000000Z"}), encoding="utf-8"
    )
    return release


def _utc(local: str) -> datetime:
    """Local CEST wall time → UTC."""

    return datetime.fromisoformat(local) - timedelta(hours=2)


def _write(
    writer: ArchiveWriter, source: str, channel: str, at: datetime, document: Any, ok: bool = True
) -> None:
    body = json.dumps(document, ensure_ascii=False).encode() if ok else None
    poll = Poll(
        requested_at=at.replace(tzinfo=UTC),
        received_at=at.replace(tzinfo=UTC),
        status=200 if ok else None,
        body=body,
        error=None if ok else "timeout",
    )
    writer.append(source, channel, poll, body)


def _duk(vehicle: int, line: int, trip: int, state: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "ID": vehicle,
        "CISLineID": line,
        "RouteID": trip,
        "State": state,
        "Delay": 0,
        "GPSPositionDT": "2026-10-06T07:09:50+02:00",
        "ArrivalDT": "1970-01-01T02:00:00+02:00",
        **extra,
    }


def _arriva(updated: str, next_stop: str, delay: str) -> dict[str, Any]:
    return {
        "spz": "7AT9086   ",
        "linkNumber": "157710",
        "destinationName": "Litvínov,,nádraží",
        "lastStopName": next_stop,
        "delay": delay,
        "updated": f"{updated}.000+00:00",
        "state": "v pohybu",
    }


def _archive(root: Path) -> Path:
    archive = root / "rt-raw"
    writer = ArchiveWriter(archive)
    morning = {
        "VehicleList": [
            _duk(807, 582492, 143),
            _duk(808, 582492, 150),
            _duk(809, 999999, 1),
            _duk(810, 582492, 777),
            _duk(20001, 0, 16001),
            _duk(1, 582492, 143, state=255),
        ]
    }
    _write(
        writer,
        "duk",
        "vehicles",
        _utc("2026-10-06T00:40:00"),
        {"VehicleList": [_duk(813, 582492, 900)]},
    )
    _write(
        writer,
        "duk",
        "vehicles",
        _utc("2026-10-06T03:00:00"),
        {"VehicleList": [_duk(812, 582492, 143, state=3)]},
    )
    _write(writer, "duk", "vehicles", _utc("2026-10-06T07:10:00"), morning)
    _write(writer, "duk", "vehicles", _utc("2026-10-06T07:10:15"), morning)  # duplicate payload
    _write(writer, "duk", "vehicles", _utc("2026-10-06T07:10:30"), None, ok=False)
    _write(
        writer,
        "duk",
        "vehicles",
        _utc("2026-10-06T08:50:00"),
        {"VehicleList": [_duk(807, 582492, 143)]},
    )
    _write(writer, "duk", "vehicles", _utc("2026-10-06T08:50:15"), {"unexpected": True})
    sz = {
        "md": "06.10.2026 07:10:00",
        "result": [
            {"properties": {"id": f"TR/{core}/2026/20261006", "tn": number, "tt": "Os", "s": 0}}
            for core, number in [
                ("3189/KASO---57501/00", "16001"),
                ("3246/AMBIG/00", "1011"),
                ("9999/NOPE/00", "1"),
            ]
        ],
    }
    _write(writer, "sz-mapa", "trains", _utc("2026-10-06T07:10:00"), sz)
    for updated, next_stop, delay in [
        ("2026-10-06T12:01:00", "Litvínov,,nádraží", "1"),
        ("2026-10-06T12:20:00", "Litvínov,,nádraží", "1"),
    ]:
        _write(
            writer,
            "arriva-express",
            "buses",
            _utc(updated),
            [{"data": {"busesCurrentLocations": [_arriva(updated, next_stop, delay)]}}],
        )
    index = day_directory(archive, "duk", "vehicles", DAY) / "index.jsonl"
    with index.open("a", encoding="utf-8") as handle:
        handle.write('{"received_at": "2026-10-06T')  # truncated by a killed recorder
    return archive


def _options(tmp_path: Path, out: str = "out") -> replay.ReplayOptions:
    return replay.ReplayOptions(
        release=_release(tmp_path) if not (tmp_path / "release").exists() else tmp_path / "release",
        archive=_archive(tmp_path) if not (tmp_path / "rt-raw").exists() else tmp_path / "rt-raw",
        start=date(2026, 10, 5),
        end=DAY,
        channels=[("duk", "vehicles"), ("sz-mapa", "trains"), ("arriva-express", "buses")],
        out=tmp_path / out,
    )


def _episodes(out: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    table = pq.read_table(out / "episodes.parquet")  # pyright: ignore[reportUnknownMemberType]
    rows = table.to_pylist()
    return {(row["source"], row["vehicle"], row["episode"]): row for row in rows}


def test_replay_resolves_each_scenario(tmp_path: Path) -> None:
    document = replay.replay(_options(tmp_path), report=lambda _: None)
    episodes = _episodes(tmp_path / "out")

    def status(source: str, vehicle: str, number: int = 0) -> tuple[str, str | None]:
        row = episodes[(source, vehicle, number)]
        return row["status"], row["trip_id"]

    assert status("duk", "807") == ("unique", "t-day")
    assert status("duk", "807", 1) == ("unique", "t-day")
    assert episodes[("duk", "807", 1)]["repeat_of"] == "807#0"
    assert episodes[("duk", "807", 0)]["repeat_of"] is None
    assert status("duk", "808") == ("not_active", None)
    assert status("duk", "809") == ("no_line", None)
    assert status("duk", "810") == ("no_trip", None)
    assert status("duk", "812") == ("wrong_time", None)
    assert status("duk", "813") == ("unique", "t-night")
    assert episodes[("duk", "813", 0)]["operating_date"] == date(2026, 10, 5)
    assert status("duk", "20001") == ("unique", "c-16001")
    assert episodes[("duk", "20001", 0)]["run_key"] == "run-16001"
    assert ("duk", "1", 0) not in episodes

    assert status("sz-mapa", "TR/3189/KASO---57501/00/2026/20261006") == ("unique", "c-16001")
    ambiguous_tr = episodes[("sz-mapa", "TR/3246/AMBIG/00/2026/20261006", 0)]
    assert (ambiguous_tr["status"], ambiguous_tr["method"], ambiguous_tr["trip_id"]) == (
        "unique",
        "czptt:train_number",
        "c-1011",
    )
    assert status("sz-mapa", "TR/9999/NOPE/00/2026/20261006") == ("no_trip", None)

    bus = episodes[("arriva-express", "7AT9086", 0)]
    assert (bus["status"], bus["trip_id"], bus["candidates"]) == ("unique", "t-litvinov-noon", 2)
    assert bus["score_min"] == 0.0

    channels = document["archive"]["channels"]
    assert channels["duk/vehicles"] == {
        "polls": 7,
        "failed_polls": 1,
        "duplicate_payloads": 1,
        "bad_payloads": 1,
        "rows": 8,
        "first_errors": [channels["duk/vehicles"]["first_errors"][0]],
    }
    assert document["coverage"]["duk"]["duk/running"]["episodes"] == 6
    assert document["coverage"]["duk"]["duk/not-running"] == {
        "episodes": 1,
        "wrong_time": 1,
    }


def test_replay_is_byte_identical(tmp_path: Path) -> None:
    replay.replay(_options(tmp_path, "first"), report=lambda _: None)
    replay.replay(_options(tmp_path, "second"), report=lambda _: None)
    for name in ("report.json", "episodes.parquet"):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()


def test_cli_replay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    options = _options(tmp_path)
    code = cli.main(
        [
            "rt",
            "replay",
            "--release",
            str(options.release),
            "--archive",
            str(options.archive),
            "--from",
            "2026-10-05",
            "--to",
            "2026-10-06",
            "--sources",
            "sz-mapa",
            "--out",
            str(options.out),
        ]
    )
    assert code == 0
    assert "sz-mapa" in capsys.readouterr().out
    assert set(json.loads((options.out / "report.json").read_text("utf-8"))["coverage"]) == {
        "sz-mapa"
    }


def test_teplice_gps_time_is_utc_mislabelled() -> None:
    body = json.dumps({"VehicleList": [_duk(400001, 585102, 1), _duk(30123, 595071, 1)]}).encode()
    teplice, dpmul = decode.decode_duk(body, datetime(2026, 10, 6, 5, 10, tzinfo=UTC))
    assert teplice.fleet == "teplice"
    assert teplice.gps_at == datetime(2026, 10, 6, 7, 9, 50, tzinfo=UTC)
    assert dpmul.gps_at == datetime(2026, 10, 6, 5, 9, 50, tzinfo=UTC)
    assert teplice.arrival_at is None
    assert teplice.local == datetime(2026, 10, 6, 7, 10)


def test_arriva_updated_is_local_time() -> None:
    body = json.dumps(
        [{"data": {"busesCurrentLocations": [_arriva("2026-10-06T12:01:00", "X", "-2")]}}]
    ).encode()
    (row,) = decode.decode_arriva(body, datetime(2026, 10, 6, 10, 1, 20, tzinfo=UTC))
    assert (row.local, row.plate, row.delay) == (datetime(2026, 10, 6, 12, 1), "7AT9086", -2)


def test_episodes_split_on_key_change_and_gap() -> None:
    start = datetime(2026, 10, 6, 7, 0)
    rows = [
        ("v", "a", start),
        ("v", "a", start + timedelta(minutes=29)),
        ("v", "b", start + timedelta(minutes=30)),
        ("v", "b", start + timedelta(minutes=61)),
        ("w", "a", start),
    ]
    episodes = split_episodes(
        rows, vehicle=lambda row: row[0], key=lambda row: row[1], time=lambda row: row[2]
    )
    assert [(e.episode_id, e.key, len(e.rows)) for e in episodes] == [
        ("v#0", "a", 2),
        ("v#1", "b", 1),
        ("v#2", "b", 1),
        ("w#0", "a", 1),
    ]
