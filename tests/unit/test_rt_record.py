from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from obehy import cli
from obehy.realtime import record
from obehy.realtime.archive import ArchiveWriter, Poll, iter_polls

T0 = datetime(2026, 10, 5, 23, 59, 50, tzinfo=UTC)


def _poll(body: bytes | None, *, at: datetime = T0, status: int | None = 200) -> Poll:
    return Poll(
        requested_at=at,
        received_at=at + timedelta(milliseconds=120),
        status=status,
        body=body,
        content_type="application/json",
        headers={"date": "Mon, 05 Oct 2026 21:59:50 GMT"},
        error=None if status == 200 else f"HTTP {status}",
    )


def _channel(
    source: str = "duk",
    channel: str = "vehicles",
    *,
    interval_s: float = 15.0,
    filter: str | None = None,
) -> record.Channel:
    return record.Channel(
        source=source,
        channel=channel,
        method="GET",
        url="https://example.invalid/vehicles",
        interval_s=interval_s,
        timeout_s=10.0,
        filter=filter,
    )


def _arriva_body(*main_types: str) -> bytes:
    vehicles = [{"spz": f"1AB{index}", "mainType": kind} for index, kind in enumerate(main_types)]
    return json.dumps([{"data": {"busesCurrentLocations": vehicles}}]).encode()


def test_shipped_manifest_loads() -> None:
    channels = record.load_channels()
    assert [channel.name for channel in channels] == [
        "duk/vehicles",
        "sz-mapa/trains",
        "arriva-express/buses",
    ]
    arriva = channels[2]
    assert arriva.method == "POST"
    assert arriva.filter == "arriva-express"
    assert arriva.body is not None and b"busesCurrentLocations" in arriva.body
    assert arriva.headers["x-enviroment"] == "client"
    assert "OsVlaky" in channels[1].url


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("interval_s = 0\ntimeout_s = 5\n", "interval_s"),
        ('interval_s = 5\ntimeout_s = 5\nfilter = "nope"\n', "unknown filter"),
        ('interval_s = 5\ntimeout_s = 5\nbody = "x"\n', "only POST"),
    ],
)
def test_manifest_errors(tmp_path: Path, snippet: str, message: str) -> None:
    manifest = tmp_path / "sources.toml"
    manifest.write_text(
        'schema_version = 1\n[[channel]]\nsource = "duk"\nchannel = "vehicles"\n'
        'method = "GET"\nurl = "https://example.invalid"\n' + snippet,
        encoding="utf-8",
    )
    with pytest.raises(record.ManifestError, match=message):
        record.load_channels(manifest)


def test_manifest_rejects_duplicate_channels(tmp_path: Path) -> None:
    table = (
        '[[channel]]\nsource = "duk"\nchannel = "vehicles"\nmethod = "GET"\n'
        'url = "https://example.invalid"\ninterval_s = 5\ntimeout_s = 5\n'
    )
    manifest = tmp_path / "sources.toml"
    manifest.write_text("schema_version = 1\n" + table + table, encoding="utf-8")
    with pytest.raises(record.ManifestError, match="duplicate"):
        record.load_channels(manifest)


def test_select_channels_rejects_unknown_source() -> None:
    channels = record.load_channels()
    assert [channel.source for channel in record.select_channels(channels, ["duk"])] == ["duk"]
    with pytest.raises(record.ManifestError, match="pid"):
        record.select_channels(channels, ["pid"])


def test_identical_payloads_share_one_object(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path)
    first = writer.append("duk", "vehicles", _poll(b'{"VehicleList":[]}'), b'{"VehicleList":[]}')
    second = writer.append("duk", "vehicles", _poll(b'{"VehicleList":[]}'), b'{"VehicleList":[]}')

    assert first.sha256 == second.sha256
    assert first.new_object_bytes > 0
    assert second.new_object_bytes == 0
    day = tmp_path / "duk" / "vehicles" / "2026-10-05"
    assert len(list((day / "objects").iterdir())) == 1
    polls = list(iter_polls(tmp_path, "duk", "vehicles", date(2026, 10, 5), date(2026, 10, 5)))
    assert len(polls) == 2
    assert polls[0].body() == b'{"VehicleList":[]}'
    assert polls[0].entry["elapsed_ms"] == 120
    assert polls[0].entry["headers"] == {"date": "Mon, 05 Oct 2026 21:59:50 GMT"}


def test_error_polls_are_archived(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path)
    writer.append("duk", "vehicles", _poll(b"bad gateway", status=502), b"bad gateway")
    start = T0 - timedelta(hours=1)
    timeout = Poll(
        start, start + timedelta(seconds=20), None, None, error="TimeoutError: timed out"
    )
    writer.append("duk", "vehicles", timeout, None)

    polls = list(iter_polls(tmp_path, "duk", "vehicles", T0.date(), T0.date()))
    assert [poll.entry["status"] for poll in polls] == [502, None]
    assert polls[0].body() == b"bad gateway"
    assert polls[1].entry["error"] == "TimeoutError: timed out"
    assert polls[1].body() is None


def test_index_rolls_over_at_utc_midnight(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path)
    writer.append("duk", "vehicles", _poll(b"a"), b"a")
    writer.append("duk", "vehicles", _poll(b"b", at=T0 + timedelta(seconds=15)), b"b")

    assert (tmp_path / "duk" / "vehicles" / "2026-10-05" / "index.jsonl").is_file()
    assert (tmp_path / "duk" / "vehicles" / "2026-10-06" / "index.jsonl").is_file()
    polls = iter_polls(tmp_path, "duk", "vehicles", date(2026, 10, 5), date(2026, 10, 6))
    assert [poll.body() for poll in polls] == [b"a", b"b"]


def test_truncated_last_index_line_is_skipped(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path)
    writer.append("duk", "vehicles", _poll(b"a"), b"a")
    index = tmp_path / "duk" / "vehicles" / "2026-10-05" / "index.jsonl"
    with index.open("a", encoding="utf-8") as stream:
        stream.write('{"requested_at": "2026-')

    polls = list(iter_polls(tmp_path, "duk", "vehicles", T0.date(), T0.date()))
    assert [poll.body() for poll in polls] == [b"a"]


def test_arriva_filter_keeps_only_express() -> None:
    channel = _channel("arriva-express", "buses", filter="arriva-express")
    source = _arriva_body("ARRIVA EXPRESS", "REGIONÁLNÍ BUS", "MHD", "ARRIVA EXPRESS")

    body, extra = record.archived_payload(channel, _poll(source))

    assert body is not None
    vehicles = json.loads(body)[0]["data"]["busesCurrentLocations"]
    assert [vehicle["spz"] for vehicle in vehicles] == ["1AB0", "1AB3"]
    assert "REGIONÁLNÍ".encode() not in body
    assert extra["filter"] == "arriva-express@1"
    assert extra["kept"] == 2
    assert extra["dropped"] == 2
    assert extra["source_bytes"] == len(source)
    assert len(str(extra["source_sha256"])) == 64


def test_arriva_filter_stores_unexpected_shape_unfiltered() -> None:
    channel = _channel("arriva-express", "buses", filter="arriva-express")
    source = b'{"errors":[{"message":"schema changed"}]}'

    body, extra = record.archived_payload(channel, _poll(source))

    assert body == source
    assert "GraphQL batch" in str(extra["filter_error"])


def test_backoff_doubles_after_repeated_failures() -> None:
    channel = _channel(interval_s=30.0)
    assert record.backoff_interval(channel, 4) == 30.0
    assert record.backoff_interval(channel, 5) == 60.0
    assert record.backoff_interval(channel, 6) == 120.0
    assert record.backoff_interval(channel, 20) == record.MAX_BACKOFF_S


def test_parse_duration() -> None:
    assert record.parse_duration("90s") == 90
    assert record.parse_duration("6h") == 6 * 3600
    assert record.parse_duration("2d") == 2 * 86400
    with pytest.raises(ValueError, match="duration"):
        record.parse_duration("6 hours")


def test_record_once_writes_one_poll_per_channel(tmp_path: Path) -> None:
    channels = [
        _channel(),
        _channel("arriva-express", "buses", filter="arriva-express"),
    ]
    payloads = {
        "duk/vehicles": b'{"VehicleList":[]}',
        "arriva-express/buses": _arriva_body("ARRIVA EXPRESS", "MHD"),
    }

    def fetcher(channel: record.Channel) -> Poll:
        return _poll(payloads[channel.name])

    stats = asyncio.run(record.record(channels, tmp_path, once=True, fetcher=fetcher))

    assert {name: value.polls for name, value in stats.items()} == {
        "duk/vehicles": 1,
        "arriva-express/buses": 1,
    }
    (arriva,) = iter_polls(tmp_path, "arriva-express", "buses", T0.date(), T0.date())
    assert arriva.entry["kept"] == 1
    (duk,) = iter_polls(tmp_path, "duk", "vehicles", T0.date(), T0.date())
    assert duk.body() == payloads["duk/vehicles"]


def test_cli_rejects_unknown_source(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["rt", "record", "--once", "--sources", "pid"]) == 1
    assert "Unknown source" in capsys.readouterr().err
