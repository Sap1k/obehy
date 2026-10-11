from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from obehy import cli
from obehy.realtime import record
from obehy.realtime.archive import ArchiveWriter, Poll, iter_polls
from obehy.realtime.model import SourceSemantics
from obehy.realtime.runtime import fetch

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


def test_shipped_manifests_load() -> None:
    channels = record.load_channels()
    assert [channel.name for channel in channels] == [
        "arriva-express/buses",
        "dpmd/vehicles",
        "duk/vehicles",
        "mpvnet/idol",
        "mpvnet/jikord",
        "mpvnet/odis",
        "mpvnet/zlin",
        "sz-mapa/trains",
        "sz-tabule/board",
    ]
    arriva, dpmd, duk, idol, *_, sz, boards = channels
    assert (dpmd.filter, dpmd.method) == ("dpmd", "GET") and "{epoch_ms}" in dpmd.url
    assert idol.headers["origin"] == "https://www.mpvnet.cz" and idol.feeds == ("jdf", "czptt")
    assert (boards.demand, boards.egress, boards.method) == ("station_boards", "sz", "POST")
    assert boards.body is not None and b"SR70={sr70}" in boards.body
    assert arriva.method == "POST"
    assert arriva.filter == "arriva-express"
    assert arriva.body is not None and b"busesCurrentLocations" in arriva.body
    assert arriva.headers["x-enviroment"] == "client"
    assert "OsVlaky" in sz.url
    assert sz.egress == "sz" and duk.egress is None and arriva.egress is None
    assert (duk.interval_s, duk.backoff_after, duk.max_backoff_s) == (15.0, 5, 300.0)
    assert duk.feeds == ("jdf", "czptt")
    assert duk.semantics["key_namespaces"]["jdf"] == "cis:line_trip"
    # DUK-Q14 and Q15 are DÚK's readings; the other sources take their facts at face value.
    assert duk.core_semantics == SourceSemantics(
        pre_trip_delay_is_elapsed=True, pre_trip_after_end_is_stale=True
    )
    assert arriva.core_semantics == SourceSemantics()
    assert sz.core_semantics == SourceSemantics(unchanged_entry_is_no_news=True)  # SZ-Q6


CHANNEL = (
    'source = "duk"\nmanifest_version = 1\n[[channel]]\nname = "vehicles"\n'
    "backoff = { after_failures = 5, max_s = 300 }\ntimeout_s = 5\n"
)
GET = 'request = { method = "GET", url = "https://x.invalid" }\n'
EVERY_5S = 'poll = { kind = "interval", seconds = 5 }\n'


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ('poll = { kind = "interval", seconds = 0 }\n' + GET, "seconds"),
        ('poll = { kind = "cron", seconds = 5 }\n' + GET, "poll.kind"),
        (EVERY_5S + 'filter = "nope"\n' + GET, "unknown filter"),
        (
            EVERY_5S + 'request = { method = "GET", url = "https://x.invalid", body = "x" }\n',
            "only POST",
        ),
        (EVERY_5S + 'feeds = ["gtfs"]\n' + GET, "unknown feeds"),
        (EVERY_5S + 'feeds = ["jdf"]\nlive_feeds = ["czptt"]\n' + GET, "unknown live_feeds"),
        (EVERY_5S + GET + "semantics = { pre_trip_after_end_is_stale = 1 }\n", "true or false"),
        (EVERY_5S + GET + 'egress = "SZ proxy"\n', "egress"),
    ],
)
def test_manifest_errors(tmp_path: Path, snippet: str, message: str) -> None:
    manifest = tmp_path / "duk.toml"
    manifest.write_text(CHANNEL + snippet, encoding="utf-8")
    with pytest.raises(record.ManifestError, match=message):
        record.load_channels(manifest)


def test_manifest_source_must_match_its_file(tmp_path: Path) -> None:
    manifest = tmp_path / "other.toml"
    manifest.write_text(CHANNEL + EVERY_5S + GET, encoding="utf-8")
    with pytest.raises(record.ManifestError, match="file name"):
        record.load_channels(manifest)


def test_manifest_rejects_duplicate_channels(tmp_path: Path) -> None:
    table = (
        '[[channel]]\nname = "vehicles"\n'
        + EVERY_5S
        + "backoff = { after_failures = 5, max_s = 300 }\ntimeout_s = 5\n"
        + GET
    )
    manifest = tmp_path / "duk.toml"
    manifest.write_text('source = "duk"\nmanifest_version = 1\n' + table + table, encoding="utf-8")
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


DPMD_BODY = (
    '/* {"TimeStamp":"2026-10-11T17:30:16.391"} */'
    " busstops=[{id:`c8e1d1e7`,coords:`50.1N, 14.2E`}];"
    " buspositions=[{id:`3f2172e3`,line:`201`,vehicle:`842`,driver:`1103`,course:`7115`,"
    "header:`<span>Autobus: 842</span>`,body:`<br>Čas: 11.10.2026 17:30:03<br>Řidič: 1103"
    '<br>Linka: 201<br>Spoj: 351<br><img src="`+vehicleImageUrl + `842.jpg" '
    "onclick=\"window.open('x')\">`,hint:`Autobus: 842\nLinka: 201\n`,fuel:`C`,arrow:`85`,"
    "coords:`50.78264999N, 14.22869015E`,final1: {name:`Chrochvice`,coords:`50.7N, 14.1E`},"
    " final2: null, shapes:`<gpx></gpx>`,off: 0},"
    " {id:`aa`,line:``,vehicle:`900`,driver:`77`,body:`<br>Autobus: 900`,off: 1}];"
    " legends=[{id:`c5`,text:`Jízda načas`,mark:``}]; datahash='0b221b33';"
).encode()


def test_dpmd_filter_keeps_vehicles_in_service_without_drivers() -> None:
    # DPMD-Q2 (JavaScript literals), DPMD-Q5 (parked vehicles), DPMD-Q8 (driver numbers).
    channel = _channel("dpmd", "vehicles", filter="dpmd")
    body, extra = record.archived_payload(channel, _poll(DPMD_BODY))
    assert body is not None
    document = json.loads(body)
    assert (document["TimeStamp"], document["datahash"]) == ("2026-10-11T17:30:16.391", "0b221b33")
    (bus,) = document["buspositions"]
    assert (bus["line"], bus["vehicle"], bus["course"]) == ("201", "842", "7115")
    assert bus["body"] == (
        "<br>Čas: 11.10.2026 17:30:03<br>Linka: 201<br>Spoj: 351"
        '<br><img src="${vehicleImageUrl}842.jpg" onclick="window.open(\'x\')">'
    )
    assert bus["final1"]["name"] == "Chrochvice" and bus["final2"] is None
    assert not {"driver", "header", "hint", "shapes"} & set(bus)
    assert b"1103" not in body and "Řidič".encode() not in body and b"busstops" not in body
    assert (extra["filter"], extra["kept"], extra["dropped"]) == ("dpmd@1", 1, 1)


def test_dpmd_filter_stores_unexpected_shape_unfiltered() -> None:
    channel = _channel("dpmd", "vehicles", filter="dpmd")
    source = b'/* {"TimeStamp":"2026-10-11T17:30:16"} */ buspositions=eval(`x`);'
    body, extra = record.archived_payload(channel, _poll(source))
    assert body == source
    assert "eval" in str(extra["filter_error"])


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.status = 200
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.urls: list[str] = []

    def open(self, request: object, timeout: float) -> _Response:
        del timeout
        self.urls.append(request.full_url)  # type: ignore[attr-defined]
        return self.response


def test_a_cache_busted_gzip_channel_is_stored_decompressed() -> None:
    # DPMD-Q9: `{epoch_ms}` is filled per request; a gzip body is stored as plain bytes.
    channel = record.Channel(
        source="dpmd",
        channel="vehicles",
        method="GET",
        url="https://x.invalid/TrafficState.js?v={epoch_ms}",
        interval_s=10.0,
        timeout_s=5.0,
        headers={"accept-encoding": "gzip"},
    )
    opener = _Opener(_Response(gzip.compress(b"plain"), {"Content-Encoding": "gzip"}))
    request = fetch.channel_request(channel, anonymous=True)
    poll = fetch.send(request, 5.0, opener=opener)  # type: ignore[arg-type]
    assert poll.body == b"plain"
    (url,) = opener.urls
    stamp = url.rsplit("=", 1)[1]
    assert stamp.isdigit() and len(stamp) == 13


def test_backoff_doubles_after_repeated_failures() -> None:
    channel = _channel(interval_s=30.0)
    assert record.backoff_interval(channel, 4) == 30.0
    assert record.backoff_interval(channel, 5) == 60.0
    assert record.backoff_interval(channel, 6) == 120.0
    assert record.backoff_interval(channel, 20) == channel.max_backoff_s


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


def test_replay_cli_reads_the_shipped_manifests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OBEHY_DATABASE_URL", raising=False)
    # No database configured: the command must get past manifest loading to fail on the URL.
    code = cli.main(
        [
            "rt",
            "replay",
            "--release",
            "x",
            "--from",
            "2026-10-05",
            "--to",
            "2026-10-05",
            "--out",
            str(tmp_path),
            "--config",
            str(tmp_path / "missing.toml"),
        ]
    )
    assert code == 1
    error = capsys.readouterr().err
    assert "database" in error.lower() or "missing.toml" in error
    assert "filter" not in error
