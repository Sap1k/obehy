# pyright: reportUnusedFunction=false
from __future__ import annotations

import gzip
import json
import zipfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from obehy import national_czptt
from obehy.national_czptt import BuildConfig, PipelineError, SourceRecord


@pytest.fixture(autouse=True)
def _configured_osm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "geodata").mkdir(exist_ok=True)

    def valid_snapshot(_osm: Path, _workdir: Path) -> dict[str, object]:
        return {"merge_key": "fixture-osm"}

    monkeypatch.setattr(
        national_czptt,
        "validate_snapshot",
        valid_snapshot,
    )

    def validate_railway_osm(_workdir: Path, source_key: str) -> Path:
        assert source_key == "fixture-osm"
        destination = tmp_path / "osm" / "railway-locations.osm.pbf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"railway-osm")
        return destination

    monkeypatch.setattr(
        national_czptt,
        "validate_railway_locations",
        validate_railway_osm,
    )


def _xml(
    *,
    core: str = "000000001234",
    variant: str = "00",
    root: str = "CZPTTCISMessage",
) -> bytes:
    if root == "CZCanceledPTTMessage":
        return (
            f"<{root}><PlannedTransportIdentifiers><ObjectType>PA</ObjectType>"
            f"<Company>54</Company><Core>{core}</Core><Variant>{variant}</Variant>"
            "<TimetableYear>2026</TimetableYear></PlannedTransportIdentifiers>"
            "<CZPTTCancelation>2025-12-14T00:00:00</CZPTTCancelation>"
            "<PlannedCalendar><BitmapDays>1</BitmapDays><ValidityPeriod>"
            "<StartDateTime>2025-12-14T00:00:00</StartDateTime></ValidityPeriod>"
            f"</PlannedCalendar></{root}>"
        ).encode()
    return (
        f"<{root}><Identifiers>"
        "<PlannedTransportIdentifiers><ObjectType>TR</ObjectType><Company>54</Company>"
        f"<Core>{core}</Core><Variant>{variant}</Variant>"
        "<TimetableYear>2026</TimetableYear></PlannedTransportIdentifiers>"
        "<PlannedTransportIdentifiers><ObjectType>PA</ObjectType><Company>54</Company>"
        f"<Core>{core}</Core><Variant>{variant}</Variant>"
        "<TimetableYear>2026</TimetableYear></PlannedTransportIdentifiers>"
        "</Identifiers><CZPTTCreation>2025-12-01T00:00:00</CZPTTCreation>"
        "<CZPTTInformation><PlannedCalendar><BitmapDays>1</BitmapDays><ValidityPeriod>"
        "<StartDateTime>2025-12-14T00:00:00</StartDateTime></ValidityPeriod>"
        "</PlannedCalendar></CZPTTInformation>"
        f"</{root}>"
    ).encode()


def _source_snapshot(root: Path) -> Path:
    sources = root / "sources"
    annual = sources / "annual" / "JR2026.zip"
    annual.parent.mkdir(parents=True)
    with zipfile.ZipFile(annual, "w") as archive:
        archive.writestr("base.xml", _xml())
    change = sources / "changes" / "2025-12" / "cancel.xml.zip"
    change.parent.mkdir(parents=True)
    change.write_bytes(gzip.compress(_xml(root="CZCanceledPTTMessage"), mtime=0))
    catalog = sources / "kadr" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        '{"companies":[],"ids":[],"lines":[],"schema_version":1}\n',
        encoding="utf-8",
    )
    sr70 = sources / "sr70" / "SR70.csv"
    sr70.parent.mkdir(parents=True)
    sr70.write_text("570760,Praha,50.083,14.435\n", encoding="utf-8")
    (sr70.parent / "SR70_Nazev20.csv").write_text(
        "570760,Praha hl.n.,50.083,14.435\n",
        encoding="utf-8",
    )
    records = [
        SourceRecord(
            relative_path="annual/JR2026.zip",
            url=None,
            bytes=annual.stat().st_size,
            sha256=national_czptt.file_digest(annual),
            kind="annual_zip",
        ),
        SourceRecord(
            relative_path="changes/2025-12/cancel.xml.zip",
            url=None,
            bytes=change.stat().st_size,
            sha256=national_czptt.file_digest(change),
            kind="monthly_gzip",
        ),
    ]
    (sources / "sources.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timetable_year": 2026,
                "objects": [record.__dict__ for record in records],
            }
        ),
        encoding="utf-8",
    )
    return sources


def test_gvd_year_changes_on_second_sunday_of_december() -> None:
    prague = ZoneInfo("Europe/Prague")
    assert (
        national_czptt.automatic_timetable_year(datetime(2025, 12, 13, 23, 59, tzinfo=prague))
        == 2025
    )
    assert (
        national_czptt.automatic_timetable_year(datetime(2025, 12, 14, 0, 0, tzinfo=prague)) == 2026
    )
    assert (
        national_czptt.automatic_timetable_year(datetime(2026, 7, 23, 12, 0, tzinfo=prague)) == 2026
    )


def test_discovery_includes_change_months_from_both_calendar_years(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "https://example.test/szdc"
    listings = {
        f"{base}/2026/": (
            b'<a href="JR2026.zip">annual</a>'
            b'<a href="2025-12/">old year</a>'
            b'<a href="2026-01/">new year</a>'
        ),
        f"{base}/2026/2025-12/": b'<a href="old.xml.zip">old</a>',
        f"{base}/2026/2026-01/": b'<a href="new.xml.gz">new</a>',
    }

    def read_url(url: str, **_kwargs: object) -> bytes:
        return listings[url]

    monkeypatch.setattr(national_czptt, "_read_url", read_url)
    inventory = national_czptt.discover_remote_inventory(base, 2026)
    assert [item.relative_path for item in inventory] == [
        "annual/JR2026.zip",
        "changes/2025-12/old.xml.zip",
        "changes/2026-01/new.xml.gz",
    ]
    assert all(item.bytes is None and item.last_modified is None for item in inventory)


def test_ftp_source_is_rejected_with_https_mirror_guidance() -> None:
    with pytest.raises(PipelineError, match="official HTTPS mirror"):
        national_czptt.discover_remote_inventory("ftp://ftp.example.test/root", 2026)

    assert national_czptt.DEFAULT_SOURCE_BASE_URL.startswith("https://portal.cisjr.cz/")


def test_http_downloader_reuses_connection_and_streams_hashed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        "/2026/one.xml.zip": b"first",
        "/2026/two.xml.zip": b"second",
    }
    connections: list[FakeConnection] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def getheader(self, name: str) -> str | None:
            return str(len(self.payload)) if name == "Content-Length" else None

        def read(self, _size: int) -> bytes:
            payload, self.payload = self.payload, b""
            return payload

        def close(self) -> None:
            return

    class FakeConnection:
        def __init__(self, host: str, port: int, *, timeout: int) -> None:
            assert (host, port, timeout) == ("example.test", 443, 120)
            self.target = ""
            self.closed = False
            connections.append(self)

        def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
            assert method == "GET"
            assert headers["Accept-Encoding"] == "identity"
            self.target = target

        def getresponse(self) -> FakeResponse:
            return FakeResponse(payloads[self.target])

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(national_czptt.http.client, "HTTPSConnection", FakeConnection)
    downloader = national_czptt._HttpSourceDownloader(  # pyright: ignore[reportPrivateUsage]
        tmp_path
    )
    one = downloader.download(
        national_czptt.RemoteObject(
            "changes/2026-01/one.xml.zip",
            "https://example.test/2026/one.xml.zip",
            "monthly_gzip",
        )
    )
    two = downloader.download(
        national_czptt.RemoteObject(
            "changes/2026-01/two.xml.zip",
            "https://example.test/2026/two.xml.zip",
            "monthly_gzip",
        )
    )
    downloader.close()

    assert len(connections) == 1
    assert connections[0].closed
    assert one.sha256 == national_czptt.file_digest(
        tmp_path / "changes" / "2026-01" / "one.xml.zip"
    )
    assert two.sha256 == national_czptt.file_digest(
        tmp_path / "changes" / "2026-01" / "two.xml.zip"
    )


def test_flatten_orders_annual_then_cancellation_then_addition(tmp_path: Path) -> None:
    sources = _source_snapshot(tmp_path)
    addition = sources / "changes" / "2025-12" / "add.xml.zip"
    addition.write_bytes(gzip.compress(_xml(variant="01"), mtime=0))
    annual = sources / "annual" / "JR2026.zip"
    cancellation = sources / "changes" / "2025-12" / "cancel.xml.zip"
    records = [
        SourceRecord(
            "annual/JR2026.zip",
            None,
            annual.stat().st_size,
            national_czptt.file_digest(annual),
            "annual_zip",
        ),
        SourceRecord(
            "changes/2025-12/cancel.xml.zip",
            None,
            cancellation.stat().st_size,
            national_czptt.file_digest(cancellation),
            "monthly_gzip",
        ),
    ]
    records.append(
        SourceRecord(
            relative_path="changes/2025-12/add.xml.zip",
            url=None,
            bytes=addition.stat().st_size,
            sha256=national_czptt.file_digest(addition),
            kind="monthly_gzip",
        )
    )
    output = tmp_path / "messages.zip"
    assert national_czptt.flatten_messages(sources, records, output) == 3
    with zipfile.ZipFile(output) as archive:
        roots = [
            national_czptt.ElementTree.fromstring(archive.read(name)).tag.rsplit("}", 1)[-1]
            for name in archive.namelist()
        ]
    assert roots == ["CZPTTCISMessage", "CZCanceledPTTMessage", "CZPTTCISMessage"]


def test_flatten_rejects_monthly_object_without_gzip_magic(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    malformed = sources / "changes" / "2025-12" / "bad.xml.zip"
    malformed.parent.mkdir(parents=True)
    malformed.write_bytes(_xml())
    record = SourceRecord(
        "changes/2025-12/bad.xml.zip",
        None,
        malformed.stat().st_size,
        national_czptt.file_digest(malformed),
        "monthly_gzip",
    )
    with pytest.raises(PipelineError, match="gzip magic"):
        national_czptt.flatten_messages(sources, [record], tmp_path / "messages.zip")


def test_flatten_rejects_conflicting_full_pa_identity(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    annual = sources / "annual" / "JR2026.zip"
    annual.parent.mkdir(parents=True)
    with zipfile.ZipFile(annual, "w") as archive:
        archive.writestr("one.xml", _xml())
        archive.writestr("two.xml", _xml().replace(b"CZPTTCreation", b"CZPTTCreation"))
    # Make the payload different without changing the full PA identity.
    with zipfile.ZipFile(annual, "a") as archive:
        changed = _xml().replace(b"</CZPTTCISMessage>", b" \n</CZPTTCISMessage>")
        archive.writestr("three.xml", changed)
    record = SourceRecord(
        "annual/JR2026.zip",
        None,
        annual.stat().st_size,
        national_czptt.file_digest(annual),
        "annual_zip",
    )
    with pytest.raises(PipelineError, match="Conflicting"):
        national_czptt.flatten_messages(sources, [record], tmp_path / "messages.zip")


def test_offline_build_uses_snapshot_and_defaults_internal_points_to_gtfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _source_snapshot(tmp_path / "snapshot")
    jrutil = tmp_path / "jrutil"
    jrutil.mkdir()
    (jrutil / ".git").mkdir()
    output = tmp_path / "result"
    commands: list[list[str]] = []

    def no_network(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("offline build attempted network access")

    monkeypatch.setattr(national_czptt, "_read_url", no_network)

    def git_identity(_path: Path) -> dict[str, object]:
        return {"commit": "abc123", "dirty": False, "status": []}

    monkeypatch.setattr(national_czptt, "git_identity", git_identity)

    def command_runner(
        command: Sequence[str],
        _cwd: Path,
        _log: Path,
        _reporter: object,
        _progress: object,
    ) -> None:
        commands.append(list(command))
        if "czptt-to-bundle" not in command:
            return
        bundle = Path(command[-1])
        gtfs = bundle / "gtfs-intermediate"
        gtfs.mkdir(parents=True)
        (gtfs / "stops.txt").write_text("stop_id,stop_name\n", encoding="utf-8")
        (gtfs / "routes.txt").write_text("route_id\n", encoding="utf-8")
        (gtfs / "trips.txt").write_text("trip_id\n", encoding="utf-8")
        (gtfs / "stop_times.txt").write_text("trip_id,stop_sequence\n", encoding="utf-8")
        (gtfs / "transfers.txt").write_text(
            "from_trip_id,to_trip_id,transfer_type\n", encoding="utf-8"
        )
        extensions = bundle / "extensions"
        extensions.mkdir()
        (extensions / "cz_routes.txt").write_text("route_id\n", encoding="utf-8")
        (extensions / "cz_trips.txt").write_text("trip_id\n", encoding="utf-8")
        (extensions / "cz_trip_stop_zones.txt").write_text(
            "trip_id,stop_sequence\n", encoding="utf-8"
        )
        for filename in national_czptt.PARQUET_FILES:
            (bundle / filename).write_bytes(b"PAR1")
        (bundle / "diagnostics.json").write_text("{}\n", encoding="utf-8")
        (bundle / "manifest.json").write_text('{"schema_version":1}\n', encoding="utf-8")

    result = national_czptt.build(
        BuildConfig(
            output=output,
            workdir=tmp_path / "workdir",
            osm_file=tmp_path / "regional.osm.pbf",
            geodata_root=tmp_path / "geodata",
            jrutil_root=jrutil,
            jrutil_command=None,
            timetable_year=2026,
            source_snapshot=snapshot,
            sr70=None,
            progress="off",
        ),
        command_runner=cast(national_czptt.CommandFn, command_runner),
    )
    assert result == output.resolve()
    converter = next(command for command in commands if "czptt-to-bundle" in command)
    assert "--operational-points=gtfs" in converter
    assert any(argument.startswith("--sr70-name20=") for argument in converter)
    assert (output / "bundle" / "operational_calls.parquet").is_file()
    assert (output / "sources" / "sr70" / "SR70_Nazev20.csv").is_file()
    manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["operational_points"] == "gtfs"
    assert manifest["jrutil"]["git"]["commit"] == "abc123"
    assert manifest["sr70_sha256"] == national_czptt.file_digest(
        output / "sources" / "sr70" / "SR70.csv"
    )
    assert manifest["sr70_name20_sha256"] == national_czptt.file_digest(
        output / "sources" / "sr70" / "SR70_Nazev20.csv"
    )

    repeated = tmp_path / "result-repeated"
    national_czptt.build(
        BuildConfig(
            output=repeated,
            workdir=tmp_path / "workdir",
            osm_file=tmp_path / "regional.osm.pbf",
            geodata_root=tmp_path / "geodata",
            jrutil_root=jrutil,
            jrutil_command=None,
            timetable_year=2026,
            source_snapshot=snapshot,
            progress="off",
        ),
        command_runner=cast(national_czptt.CommandFn, command_runner),
    )
    first_files = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    repeated_files = {
        path.relative_to(repeated).as_posix(): path.read_bytes()
        for path in repeated.rglob("*")
        if path.is_file()
    }
    assert repeated_files == first_files


def test_source_snapshot_and_custom_remote_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="forbids"):
        national_czptt.build(
            BuildConfig(
                output=tmp_path / "out",
                workdir=tmp_path / "workdir",
                osm_file=tmp_path / "regional.osm.pbf",
                geodata_root=tmp_path / "geodata",
                jrutil_root=tmp_path,
                jrutil_command=None,
                source_snapshot=tmp_path,
                source_base_url="https://example.test/czptt",
            )
        )


def test_source_snapshot_requires_the_sr70_pair(tmp_path: Path) -> None:
    snapshot = _source_snapshot(tmp_path / "snapshot")
    (snapshot / "sr70" / "SR70_Nazev20.csv").unlink()
    jrutil = tmp_path / "jrutil"
    jrutil.mkdir()

    with pytest.raises(PipelineError, match="must contain both"):
        national_czptt.build(
            BuildConfig(
                output=tmp_path / "out",
                workdir=tmp_path / "workdir",
                osm_file=tmp_path / "regional.osm.pbf",
                geodata_root=tmp_path / "geodata",
                jrutil_root=jrutil,
                jrutil_command=None,
                timetable_year=2026,
                source_snapshot=snapshot,
                progress="off",
            )
        )


def test_sr70_name20_override_requires_coordinate_pair(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="requires --sr70"):
        national_czptt.build(
            BuildConfig(
                output=tmp_path / "out",
                workdir=tmp_path / "workdir",
                osm_file=tmp_path / "regional.osm.pbf",
                geodata_root=tmp_path / "geodata",
                jrutil_root=tmp_path,
                jrutil_command=None,
                sr70_name20=tmp_path / "SR70_Nazev20.csv",
            )
        )


def test_gtfs_stop_verifier_enforces_hierarchy_and_sr70_coordinates(
    tmp_path: Path,
) -> None:
    gtfs = tmp_path / "gtfs"
    gtfs.mkdir()
    sr70 = tmp_path / "SR70.csv"
    sr70.write_text("57076,Praha hl.n.,50.083,14.435\n", encoding="utf-8")
    stops = gtfs / "stops.txt"
    stops.write_text(
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station,platform_code\n"
        "czptt:stop:CZ:57076,Praha hl.n.,50.083,14.435,1,,\n"
        "czptt:stop:CZ:57076:unspecified,Praha hl.n.,50.083,14.435,0,"
        "czptt:stop:CZ:57076,\n"
        "czptt:stop:DE:57076,Foreign,0,0,1,,\n"
        "czptt:stop:DE:57076:platform:1,Foreign,0,0,0,czptt:stop:DE:57076,1\n",
        encoding="utf-8",
    )
    national_czptt._verify_gtfs_stops(gtfs, sr70)  # pyright: ignore[reportPrivateUsage]

    stops.write_text(
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "czptt:stop:CZ:57076,Praha hl.n.,0,0,1,\n"
        "czptt:stop:CZ:57076:unspecified,Praha hl.n.,0,0,0,czptt:stop:CZ:57076\n",
        encoding="utf-8",
    )
    with pytest.raises(PipelineError, match="does not carry its SR70 coordinates"):
        national_czptt._verify_gtfs_stops(  # pyright: ignore[reportPrivateUsage]
            gtfs, sr70
        )

    stops.write_text(
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "czptt:stop:CZ:57076:unspecified,Praha hl.n.,50.083,14.435,0,\n",
        encoding="utf-8",
    )
    with pytest.raises(PipelineError, match="lacks its station parent"):
        national_czptt._verify_gtfs_stops(  # pyright: ignore[reportPrivateUsage]
            gtfs, sr70
        )


def test_command_runtime_records_dll_hash_and_skips_checkout(tmp_path: Path) -> None:
    dll = tmp_path / "jrutil.dll"
    dll.write_bytes(b"fixture-dll")
    config = BuildConfig(
        output=tmp_path / "out",
        workdir=tmp_path / "work",
        osm_file=tmp_path / "region.osm.pbf",
        geodata_root=tmp_path / "geodata",
        jrutil_root=None,
        jrutil_command=("dotnet", str(dll)),
    )

    provenance = national_czptt._jrutil_provenance(  # pyright: ignore[reportPrivateUsage]
        config
    )

    assert provenance["mode"] == "command"
    files = cast(list[dict[str, object]], provenance["files"])
    assert files[0]["sha256"] == national_czptt.file_digest(dll)


def test_foreign_unresolved_point_requires_reviewed_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "diagnostics.json").write_text(
        json.dumps({"coordinate_diagnostics": {"unresolvedPointIds": ["czptt:stop:DE:12345"]}}),
        encoding="utf-8",
    )
    review = tmp_path / "review.json"
    review.write_text('{"residual_dispositions": {}}\n', encoding="utf-8")
    monkeypatch.setattr(national_czptt, "OSM_REVIEW_PATH", review)

    with pytest.raises(PipelineError, match="reviewed residual disposition"):
        national_czptt._verify_foreign_coordinate_acceptance(  # pyright: ignore[reportPrivateUsage]
            bundle
        )

    review.write_text(
        '{"residual_dispositions":{"czptt:stop:DE:12345":"reviewed:no-candidate"}}\n',
        encoding="utf-8",
    )
    national_czptt._verify_foreign_coordinate_acceptance(  # pyright: ignore[reportPrivateUsage]
        bundle
    )
