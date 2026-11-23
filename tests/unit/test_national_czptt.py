# pyright: reportUnusedFunction=false
from __future__ import annotations

import gzip
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import ClassVar, cast
from zoneinfo import ZoneInfo

import pytest

from obehy import gvd, national_czptt
from obehy.national_czptt import BuildConfig, RemoteObject, SourceRecord
from obehy.pipeline import download, reporting
from obehy.pipeline.errors import PipelineError


def _no_sleep(_seconds: float) -> None:
    return


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


def test_gvd_year_changes_on_second_sunday_of_december() -> None:
    prague = ZoneInfo("Europe/Prague")
    assert gvd.automatic_timetable_year(datetime(2025, 12, 13, 23, 59, tzinfo=prague)) == 2025
    assert gvd.automatic_timetable_year(datetime(2025, 12, 14, 0, 0, tzinfo=prague)) == 2026
    assert gvd.automatic_timetable_year(datetime(2026, 7, 23, 12, 0, tzinfo=prague)) == 2026


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

    monkeypatch.setattr(national_czptt, "read_url", read_url)
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


def _live_inventory_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inventories: list[list[RemoteObject]]
) -> tuple[BuildConfig, list[str], list[Path]]:
    downloads: list[str] = []
    source_roots: list[Path] = []
    discovery_calls = iter(inventories)

    def discover(_base: str, _year: int) -> list[RemoteObject]:
        return next(discovery_calls)

    monkeypatch.setattr(national_czptt, "discover_remote_inventory", discover)

    class FakeDownloader:
        def __init__(self, sources: Path, cache: Path | None = None) -> None:
            del cache
            self.sources = sources
            self.cached = 0
            source_roots.append(sources)

        def download(self, item: RemoteObject) -> SourceRecord:
            downloads.append(item.relative_path)
            destination = self.sources / item.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if item.kind == "annual_zip":
                with zipfile.ZipFile(destination, "w") as archive:
                    archive.writestr("base.xml", _xml())
            else:
                destination.write_bytes(gzip.compress(_xml(), mtime=0))
            return SourceRecord(
                relative_path=item.relative_path,
                url=item.url,
                bytes=destination.stat().st_size,
                sha256=national_czptt.file_digest(destination),
                kind=item.kind,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(national_czptt, "_HttpSourceDownloader", FakeDownloader)

    def snapshot_kadr(destination: Path) -> Path:
        destination.mkdir(parents=True)
        catalog = destination / "catalog.json"
        catalog.write_text("{}\n", encoding="utf-8")
        return catalog

    monkeypatch.setattr(national_czptt, "snapshot_kadr", snapshot_kadr)

    geodata = tmp_path / "geodata"
    rail = geodata / "rail"
    rail.mkdir(parents=True)
    (rail / "SR70.csv").write_text("570760,Praha,50.083,14.435\n", encoding="utf-8")
    config = BuildConfig(
        geodata_root=geodata,
        timetable_year=2026,
        source_base_url="https://example.test/czptt",
    )
    return config, downloads, source_roots


def test_snapshot_downloads_new_inventory_objects_until_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annual = RemoteObject("annual/JR2026.zip", "https://example.test/JR2026.zip", "annual_zip")
    first = RemoteObject(
        "changes/2026-01/first.xml.zip", "https://example.test/first.xml.zip", "monthly_gzip"
    )
    second = RemoteObject(
        "changes/2026-01/second.xml.zip", "https://example.test/second.xml.zip", "monthly_gzip"
    )
    config, downloads, source_roots = _live_inventory_fixture(
        tmp_path,
        monkeypatch,
        [[annual], [annual, first], [annual, first, second], [annual, first, second]],
    )

    national_czptt.snapshot_sources(config, tmp_path / "snapshot", reporting.BuildReporter("off"))

    assert downloads == [annual.relative_path, first.relative_path, second.relative_path]
    sources = source_roots[0]
    inventory = json.loads((sources / "inventory.json").read_text(encoding="utf-8"))
    manifest = json.loads((sources / "sources.json").read_text(encoding="utf-8"))
    assert [item["relative_path"] for item in inventory["objects"]] == downloads
    assert [item["relative_path"] for item in manifest["objects"]] == downloads


def test_snapshot_sources_writes_a_reusable_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annual = RemoteObject("annual/JR2026.zip", "https://example.test/JR2026.zip", "annual_zip")
    change = RemoteObject(
        "changes/2026-01/one.xml.zip", "https://example.test/one.xml.zip", "monthly_gzip"
    )
    config, downloads, _roots = _live_inventory_fixture(
        tmp_path, monkeypatch, [[annual, change], [annual, change]]
    )

    snapshot = national_czptt.snapshot_sources(
        config, tmp_path / "snapshot", reporting.BuildReporter("off")
    )

    assert downloads == [annual.relative_path, change.relative_path]
    manifest = json.loads((snapshot / "sources.json").read_text(encoding="utf-8"))
    assert [item["relative_path"] for item in manifest["objects"]] == downloads
    assert [item["relative_path"] for item in manifest["auxiliary"]] == [
        "kadr/catalog.json",
        "sr70/SR70.csv",
    ]


def test_http_downloader_gives_up_with_a_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    class FailingConnection:
        def __init__(self, _host: str, _port: int, *, timeout: int) -> None:
            del timeout

        def request(self, _method: str, target: str, *, headers: dict[str, str]) -> None:
            del headers
            attempts.append(target)
            raise TimeoutError("handshake timed out")

        def close(self) -> None:
            return

    monkeypatch.setattr(national_czptt.http.client, "HTTPSConnection", FailingConnection)
    monkeypatch.setattr(download, "_sleep", _no_sleep)
    downloader = national_czptt._HttpSourceDownloader(  # pyright: ignore[reportPrivateUsage]
        tmp_path
    )

    with pytest.raises(download.DownloadError, match=r"CZPTT changes/one.xml.zip failed after 3"):
        downloader.download(
            RemoteObject("changes/one.xml.zip", "https://example.test/one.xml.zip", "monthly_gzip")
        )
    assert len(attempts) == 3


def test_snapshot_fails_if_inventory_still_grows_after_five_download_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annual = RemoteObject("annual/JR2026.zip", "https://example.test/JR2026.zip", "annual_zip")
    changes = [
        RemoteObject(
            f"changes/2026-01/{index}.xml.zip",
            f"https://example.test/{index}.xml.zip",
            "monthly_gzip",
        )
        for index in range(1, 6)
    ]
    config, downloads, source_roots = _live_inventory_fixture(
        tmp_path,
        monkeypatch,
        [[annual, *changes[:index]] for index in range(6)],
    )

    with pytest.raises(PipelineError, match="after 5 download passes"):
        national_czptt.snapshot_sources(
            config, tmp_path / "snapshot", reporting.BuildReporter("off")
        )

    assert downloads == [annual.relative_path, *(item.relative_path for item in changes[:4])]
    assert not (source_roots[0] / "sources.json").exists()


def test_kadr_snapshot_preserves_central_note_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[str] = []

    def soap_response(_url: str, **kwargs: object) -> bytes:
        request = cast(bytes, kwargs["data"])
        operation = next(
            name for name in national_czptt.KADR_OPERATIONS if f"<{name} ".encode() in request
        )
        operations.append(operation)
        item = ""
        if operation == "SeznamPoznamkyKJR":
            item = (
                "<Poznamka><Kod>36</Kod><Nazev>Přeprava jízdních kol vyloučena</Nazev>"
                "<Text>Přeprava jízdních kol není povolena.</Text>"
                "<PlatnostOd>2025-12-14T00:00:00</PlatnostOd>"
                "<PlatnostDo>2026-12-12T00:00:00</PlatnostDo></Poznamka>"
            )
        return (
            '<?xml version="1.0"?><Envelope><Body><Response><Result>'
            f"{item}</Result></Response></Body></Envelope>"
        ).encode()

    monkeypatch.setattr(national_czptt, "read_url", soap_response)
    catalog_path = national_czptt.snapshot_kadr(tmp_path / "kadr")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    assert operations == list(national_czptt.KADR_OPERATIONS)
    assert catalog["central_notes"] == [
        {
            "code": "36",
            "name": "Přeprava jízdních kol vyloučena",
            "text": "Přeprava jízdních kol není povolena.",
            "valid_from": "2025-12-14",
            "valid_to": "2026-12-12",
        }
    ]
    assert (tmp_path / "kadr" / "SeznamPoznamkyKJR.xml").is_file()


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
    national_czptt.verify_gtfs_stops(gtfs, sr70)

    stops.write_text(
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "czptt:stop:CZ:57076,Praha hl.n.,0,0,1,\n"
        "czptt:stop:CZ:57076:unspecified,Praha hl.n.,0,0,0,czptt:stop:CZ:57076\n",
        encoding="utf-8",
    )
    with pytest.raises(PipelineError, match="does not carry its SR70 coordinates"):
        national_czptt.verify_gtfs_stops(gtfs, sr70)

    stops.write_text(
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "czptt:stop:CZ:57076:unspecified,Praha hl.n.,50.083,14.435,0,\n",
        encoding="utf-8",
    )
    with pytest.raises(PipelineError, match="lacks its station parent"):
        national_czptt.verify_gtfs_stops(gtfs, sr70)


class _CountingConnection:
    """HTTPS fake serving ``payloads`` by request target and recording every request."""

    payloads: ClassVar[dict[str, bytes]] = {}
    requests: ClassVar[list[str]] = []

    def __init__(self, _host: str, _port: int, *, timeout: int) -> None:
        del timeout
        self.target = ""

    def request(self, _method: str, target: str, *, headers: dict[str, str]) -> None:
        del headers
        self.target = target
        self.requests.append(target)

    def getresponse(self) -> object:
        payload = self.payloads[self.target]

        class Response:
            status = 200

            def __init__(self) -> None:
                self.payload = payload

            def getheader(self, name: str) -> str | None:
                return str(len(payload)) if name == "Content-Length" else None

            def read(self, _size: int) -> bytes:
                chunk, self.payload = self.payload, b""
                return chunk

            def close(self) -> None:
                return

        return Response()

    def close(self) -> None:
        return


def _annual_payload() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("base.xml", _xml())
    return buffer.getvalue()


def test_http_downloader_reuses_cached_objects_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = gzip.compress(_xml(), mtime=0)
    _CountingConnection.payloads = {"/JR2026.zip": _annual_payload(), "/one.xml.zip": change}
    _CountingConnection.requests = []
    monkeypatch.setattr(national_czptt.http.client, "HTTPSConnection", _CountingConnection)
    items = [
        RemoteObject("annual/JR2026.zip", "https://example.test/JR2026.zip", "annual_zip"),
        RemoteObject(
            "changes/2026-01/one.xml.zip", "https://example.test/one.xml.zip", "monthly_gzip"
        ),
    ]
    cache = tmp_path / "cache"

    first = national_czptt._HttpSourceDownloader(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "run-1", cache
    )
    first_records = [first.download(item) for item in items]
    first.close()
    second = national_czptt._HttpSourceDownloader(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "run-2", cache
    )
    second_records = [second.download(item) for item in items]
    second.close()

    assert _CountingConnection.requests == ["/JR2026.zip", "/one.xml.zip"]
    assert (first.cached, second.cached) == (0, 2)
    assert second_records == first_records
    assert (tmp_path / "run-2" / "changes" / "2026-01" / "one.xml.zip").read_bytes() == change
    assert not list(cache.rglob("*.part"))


def test_http_downloader_replaces_a_corrupt_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = gzip.compress(_xml(), mtime=0)
    _CountingConnection.payloads = {"/one.xml.zip": change}
    _CountingConnection.requests = []
    monkeypatch.setattr(national_czptt.http.client, "HTTPSConnection", _CountingConnection)
    cache = tmp_path / "cache"
    corrupt = cache / "changes" / "2026-01" / "one.xml.zip"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not gzip")
    downloader = national_czptt._HttpSourceDownloader(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "sources", cache
    )

    downloader.download(
        RemoteObject(
            "changes/2026-01/one.xml.zip", "https://example.test/one.xml.zip", "monthly_gzip"
        )
    )

    assert _CountingConnection.requests == ["/one.xml.zip"]
    assert downloader.cached == 0
    assert corrupt.read_bytes() == change


def test_source_cache_prune_keeps_only_the_current_inventory(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    kept = cache / "2026" / "changes" / "2026-01" / "kept.xml.zip"
    stale = cache / "2026" / "changes" / "2025-12" / "stale.xml.zip"
    old_year = cache / "2025" / "annual" / "JR2025.zip"
    for path in (kept, stale, old_year):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    national_czptt._prune_source_cache(  # pyright: ignore[reportPrivateUsage]
        cache,
        2026,
        [RemoteObject("changes/2026-01/kept.xml.zip", "https://example.test/k", "monthly_gzip")],
    )

    assert [path.relative_to(cache).as_posix() for path in cache.rglob("*")] == [
        "2026",
        "2026/changes",
        "2026/changes/2026-01",
        "2026/changes/2026-01/kept.xml.zip",
    ]
