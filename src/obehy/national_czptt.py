"""Fetch the national CZPTT sources into a snapshot the compiler reads, and check its stops."""

from __future__ import annotations

import concurrent.futures
import csv
import hashlib
import http.client
import json
import os
import re
import shutil
import threading
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from obehy.gvd import resolve_timetable_year
from obehy.pipeline import download
from obehy.pipeline.download import USER_AGENT, read_url
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import file_digest, link_or_copy, utc_now, write_json
from obehy.pipeline.reporting import Reporter, StageClock

DEFAULT_SOURCE_BASE_URL = "https://portal.cisjr.cz/pub/draha/celostatni/szdc"
MAX_INVENTORY_DOWNLOAD_PASSES = 5
# Change messages average under 1 KB, so downloads are round-trip bound, not bandwidth bound.
DEFAULT_DOWNLOAD_CONNECTIONS = 32
KADR_ENDPOINT = "https://provoz.spravazeleznic.cz/kadrws/ciselniky.asmx"
KADR_OPERATIONS = (
    "SeznamSpolecnosti",
    "SeznamDruhuVlaku",
    "SeznamKomercniDruhVlaku",
    "SeznamLinky",
    "SeznamIDS",
    "SeznamPoznamkyKJR",
)


@dataclass(frozen=True)
class RemoteObject:
    relative_path: str
    url: str
    kind: Literal["annual_zip", "monthly_gzip"]
    bytes: int | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class SourceRecord:
    relative_path: str
    url: str | None
    bytes: int
    sha256: str
    kind: str


@dataclass(frozen=True)
class BuildConfig:
    """What fetching the CZPTT sources needs."""

    geodata_root: Path
    timetable_year: int | Literal["auto"] = "auto"
    source_base_url: str = DEFAULT_SOURCE_BASE_URL
    sr70: Path | None = None
    # Source objects are published once and never rewritten, so a cache keyed by relative path
    # lets a run download only the objects published since the previous one.
    source_cache_dir: Path | None = None
    download_connections: int = DEFAULT_DOWNLOAD_CONNECTIONS


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.hrefs.append(value)


def _listing_names(payload: bytes) -> list[str]:
    text = payload.decode("utf-8", errors="replace")
    parser = _HrefParser()
    parser.feed(text)
    names = [PurePosixPath(value.rstrip("/")).name for value in parser.hrefs]
    if not names:
        for line in text.splitlines():
            token = line.split()[-1] if line.split() else ""
            if token not in {".", ".."}:
                names.append(PurePosixPath(token.rstrip("/")).name)
    return sorted({name for name in names if name and name not in {".", ".."}})


def _discover_url_inventory(base_url: str, timetable_year: int) -> list[RemoteObject]:
    year_url = f"{base_url.rstrip('/')}/{timetable_year}/"
    year_names = _listing_names(read_url(year_url))
    annual_name = f"JR{timetable_year}.zip"
    if annual_name not in year_names:
        raise PipelineError(f"Annual CZPTT archive is missing from discovery: {annual_name}")
    result = [
        RemoteObject(
            relative_path=f"annual/{annual_name}",
            url=urljoin(year_url, annual_name),
            kind="annual_zip",
        )
    ]
    month_pattern = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
    months = sorted(name for name in year_names if month_pattern.fullmatch(name))

    def discover_month(month: str) -> list[RemoteObject]:
        month_url = urljoin(year_url, f"{month}/")
        objects: list[RemoteObject] = []
        for filename in _listing_names(read_url(month_url)):
            if filename.casefold().endswith((".xml.zip", ".xml.gz")):
                objects.append(
                    RemoteObject(
                        relative_path=f"changes/{month}/{filename}",
                        url=urljoin(month_url, quote(filename, safe="")),
                        kind="monthly_gzip",
                    )
                )
        return objects

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(months) or 1)) as executor:
        for objects in executor.map(discover_month, months):
            result.extend(objects)
    return sorted(result, key=lambda item: item.relative_path)


def discover_remote_inventory(base_url: str, timetable_year: int) -> list[RemoteObject]:
    scheme = urlsplit(base_url).scheme.casefold()
    if scheme == "ftp":
        raise PipelineError(
            "FTP source discovery is intentionally unsupported; use the official HTTPS mirror at "
            f"{DEFAULT_SOURCE_BASE_URL}"
        )
    if scheme not in {"http", "https"}:
        raise PipelineError("--source-base-url must use HTTP or HTTPS")
    return _discover_url_inventory(base_url, timetable_year)


HttpConnection = http.client.HTTPConnection | http.client.HTTPSConnection


class _HttpSourceDownloader:
    """Stream source objects over per-worker persistent HTTP connections.

    With a cache, objects already fetched by an earlier run are linked from it instead of being
    downloaded, and newly downloaded objects are added to it.
    """

    def __init__(self, sources: Path, cache: Path | None = None) -> None:
        self.sources = sources
        self.cache = cache
        self.cached = 0
        self._cached_lock = threading.Lock()
        self._local = threading.local()
        self._clients: list[HttpConnection] = []
        self._clients_lock = threading.Lock()

    def _connection(self, scheme: str, host: str, port: int) -> HttpConnection:
        current = cast(HttpConnection | None, getattr(self._local, "connection", None))
        origin = cast(tuple[str, str, int] | None, getattr(self._local, "origin", None))
        if current is not None and origin == (scheme, host, port):
            return current
        if current is not None:
            current.close()
        connection: HttpConnection
        if scheme == "https":
            connection = http.client.HTTPSConnection(host, port, timeout=120)
        else:
            connection = http.client.HTTPConnection(host, port, timeout=120)
        self._local.connection = connection
        self._local.origin = (scheme, host, port)
        with self._clients_lock:
            self._clients.append(connection)
        return connection

    def _reset_connection(self) -> None:
        current = cast(HttpConnection | None, getattr(self._local, "connection", None))
        if current is not None:
            current.close()
        self._local.connection = None
        self._local.origin = None

    def _from_cache(self, item: RemoteObject, destination: Path) -> SourceRecord | None:
        if self.cache is None:
            return None
        cached = self.cache / item.relative_path
        if not cached.is_file():
            return None
        try:
            _validate_object(cached, item.kind)
        except PipelineError:
            cached.unlink()
            return None
        destination.unlink(missing_ok=True)
        link_or_copy(cached, destination)
        with self._cached_lock:
            self.cached += 1
        return SourceRecord(
            relative_path=item.relative_path,
            url=item.url,
            bytes=destination.stat().st_size,
            sha256=file_digest(destination),
            kind=item.kind,
        )

    def _store_in_cache(self, item: RemoteObject, destination: Path) -> None:
        if self.cache is None:
            return
        cached = self.cache / item.relative_path
        temporary = cached.with_name(cached.name + ".part")
        temporary.unlink(missing_ok=True)
        link_or_copy(destination, temporary)
        os.replace(temporary, cached)

    def download(self, item: RemoteObject) -> SourceRecord:
        parsed = urlsplit(item.url)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or parsed.hostname is None:
            raise PipelineError(f"Unsupported CZPTT source object URL: {item.url}")
        port = parsed.port or (443 if scheme == "https" else 80)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        destination = self.sources / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        cached = self._from_cache(item, destination)
        if cached is not None:
            return cached

        attempts = len(download.RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            connection = self._connection(scheme, parsed.hostname, port)
            started_at = utc_now()
            started = time.monotonic()
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    response.close()
                    raise PipelineError(
                        f"CZPTT download failed with HTTP {response.status}: {item.url}"
                    )
                expected_header = response.getheader("Content-Length")
                expected = (
                    int(expected_header)
                    if expected_header is not None and expected_header.isdigit()
                    else item.bytes
                )
                digest = hashlib.sha256()
                received = 0
                with temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                response.close()
                if expected is not None and received != expected:
                    raise PipelineError(
                        f"Discovered CZPTT object was truncated or changed: "
                        f"{item.relative_path}; expected {expected} bytes, received {received}"
                    )
                os.replace(temporary, destination)
                self._store_in_cache(item, destination)
                return SourceRecord(
                    relative_path=item.relative_path,
                    url=item.url,
                    bytes=received,
                    sha256=digest.hexdigest(),
                    kind=item.kind,
                )
            except PipelineError:
                raise
            except (http.client.HTTPException, OSError) as error:
                self._reset_connection()
                download.record_attempt(
                    f"CZPTT {item.relative_path}",
                    item.url,
                    attempt,
                    started_at,
                    time.monotonic() - started,
                    "failed" if attempt == attempts else "retry",
                    error,
                )
                if attempt == attempts:
                    raise download.DownloadError(
                        f"CZPTT {item.relative_path}", item.url, attempt, str(error)
                    ) from error
                download.sleep_before_retry(attempt)
        raise AssertionError("unreachable HTTP download retry state")

    def close(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for connection in clients:
            connection.close()


def _prune_source_cache(
    cache: Path, timetable_year: int, inventory: Sequence[RemoteObject]
) -> None:
    """Drop cached objects outside the current inventory, including other timetable years."""

    if not cache.is_dir():
        return
    year_root = cache / str(timetable_year)
    for entry in cache.iterdir():
        if entry != year_root:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    if not year_root.is_dir():
        return
    keep = {year_root / item.relative_path for item in inventory}
    for path in sorted(year_root.rglob("*"), reverse=True):
        if path.is_file() and path not in keep:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def snapshot_kadr(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    responses: dict[str, bytes] = {}
    for operation in KADR_OPERATIONS:
        parameters = (
            "<jenAktualnePlatne>false</jenAktualnePlatne>"
            if operation == "SeznamSpolecnosti"
            else "<jenAktulnePlatne>false</jenAktulnePlatne>"
            if operation == "SeznamDruhuVlaku"
            else ""
        )
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><{operation} xmlns="http://provoz.szdc.cz/kadr">'
            f"{parameters}</{operation}></soap:Body>"
            "</soap:Envelope>"
        ).encode()
        payload = read_url(
            KADR_ENDPOINT,
            data=envelope,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"http://provoz.szdc.cz/kadr/{operation}"',
            },
        )
        responses[operation] = payload
        (destination / f"{operation}.xml").write_bytes(payload)

    def rows(operation: str) -> list[dict[str, str]]:
        root = ElementTree.fromstring(responses[operation])
        result: list[dict[str, str]] = []
        for element in root.iter():
            children = list(element)
            values = dict(element.attrib)
            values.update(
                {
                    child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
                    for child in children
                    if child.text
                }
            )
            if {"Kod", "EvCisloEU", "KodTAF"} & values.keys():
                result.append(values)
        return result

    def first(value: Mapping[str, str], *names: str) -> str | None:
        return next((value[name] for name in names if value.get(name)), None)

    def catalog_date(value: Mapping[str, str], *names: str) -> str | None:
        raw = first(value, *names)
        return raw[:10] if raw else None

    lines = [
        {
            "code": value["Kod"],
            "abbreviation": first(value, "Zkratka"),
            "name": first(value, "Nazev") or "",
            "mark": first(value, "Znacka") or value["Kod"],
            "valid_from": catalog_date(value, "PlatnostOd"),
            "valid_to": catalog_date(value, "PlatnostDo"),
        }
        for value in rows("SeznamLinky")
    ]
    companies = [
        {
            "code": first(value, "EvCisloEU", "Kod") or "",
            "name": first(value, "ObchodNazev", "Nazev") or "",
            "url": first(value, "WWW"),
        }
        for value in rows("SeznamSpolecnosti")
    ]
    ids = [
        {
            "code": value["Kod"],
            "abbreviation": first(value, "Zkratka") or value["Kod"],
            "name": first(value, "Nazev") or "",
            "note": first(value, "Poznamka"),
            "valid_from": catalog_date(value, "PlatnostOd"),
            "valid_to": catalog_date(value, "PlatnostDo"),
        }
        for value in rows("SeznamIDS")
    ]
    train_types = [
        {
            "code": value["KodTAF"],
            "abbreviation": first(value, "Zkratka") or value["KodTAF"],
        }
        for value in rows("SeznamDruhuVlaku")
    ]
    commercial_train_types = [
        {
            "code": value["KodTAF"],
            "abbreviation": first(value, "Kod") or value["KodTAF"],
        }
        for value in rows("SeznamKomercniDruhVlaku")
    ]
    central_notes: list[dict[str, str | None]] = [
        {
            "code": value["Kod"],
            "name": first(value, "Nazev") or value["Kod"],
            "text": first(value, "Text"),
            "valid_from": catalog_date(value, "PlatnostOd"),
            "valid_to": catalog_date(value, "PlatnostDo"),
        }
        for value in rows("SeznamPoznamkyKJR")
    ]
    catalog = destination / "catalog.json"
    write_json(
        catalog,
        {
            "schema_version": 1,
            "lines": sorted(lines, key=lambda value: cast(str, value["code"])),
            "companies": sorted(companies, key=lambda value: cast(str, value["code"])),
            "ids": sorted(ids, key=lambda value: cast(str, value["code"])),
            "train_types": sorted(train_types, key=lambda value: value["code"]),
            "commercial_train_types": sorted(
                commercial_train_types, key=lambda value: value["code"]
            ),
            "central_notes": sorted(central_notes, key=lambda value: cast(str, value["code"])),
        },
    )
    return catalog


def _validate_object(path: Path, kind: str) -> None:
    if kind == "annual_zip":
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic != b"PK\x03\x04":
            raise PipelineError(f"Annual CZPTT object lacks ZIP magic: {path}")
        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
                if bad is not None:
                    raise PipelineError(f"Corrupt annual CZPTT ZIP member: {bad}")
        except zipfile.BadZipFile as error:
            raise PipelineError(f"Malformed annual CZPTT ZIP: {path}") from error
    else:
        with path.open("rb") as stream:
            magic = stream.read(2)
        if magic != b"\x1f\x8b":
            raise PipelineError(f"Monthly CZPTT object lacks gzip magic: {path}")


def _finalize_sources_manifest(sources: Path) -> None:
    path = sources / "sources.json"
    value = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    auxiliary: list[dict[str, object]] = []
    for item in sorted(
        candidate
        for root in (sources / "kadr", sources / "sr70")
        if root.is_dir()
        for candidate in root.rglob("*")
        if candidate.is_file()
    ):
        auxiliary.append(
            {
                "relative_path": item.relative_to(sources).as_posix(),
                "bytes": item.stat().st_size,
                "sha256": file_digest(item),
            }
        )
    value["auxiliary"] = auxiliary
    write_json(path, value)


def _sr70_coordinates(path: Path) -> dict[str, tuple[float, float]]:
    grouped: dict[str, set[tuple[float, float]]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for fields in csv.reader(stream):
            if len(fields) < 4 or len(fields[0]) < 5:
                continue
            try:
                coordinates = (float(fields[-2]), float(fields[-1]))
            except ValueError:
                continue
            grouped.setdefault(fields[0][:5], set()).add(coordinates)
    return {code: next(iter(values)) for code, values in grouped.items() if len(values) == 1}


def _czptt_stop_identity(stop_id: str) -> tuple[str, str, str]:
    prefix = "czptt:stop:"
    if not stop_id.startswith(prefix):
        raise PipelineError(f"Unexpected CZPTT stop_id namespace: {stop_id}")
    components = stop_id[len(prefix) :].split(":")
    if len(components) < 2:
        raise PipelineError(f"Malformed CZPTT stop_id: {stop_id}")
    country = unquote(components[0])
    primary_code = unquote(components[1])
    parent_id = f"{prefix}{components[0]}:{components[1]}"
    return country, primary_code, parent_id


def verify_gtfs_stops(gtfs: Path, sr70_path: Path) -> None:
    """Check the station hierarchy and SR70 coordinates of a CZPTT package's GTFS stops."""

    stops_path = gtfs / "stops.txt"
    if not stops_path.is_file():
        raise PipelineError("CZPTT package GTFS is missing stops.txt")
    with stops_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        return

    by_id = {row.get("stop_id", ""): row for row in rows}
    if "" in by_id:
        raise PipelineError("CZPTT GTFS contains an empty stop_id")
    if len(by_id) != len(rows):
        raise PipelineError("CZPTT GTFS contains duplicate stop_id values")
    sr70 = _sr70_coordinates(sr70_path)
    for stop_id, row in by_id.items():
        country, primary_code, expected_parent = _czptt_stop_identity(stop_id)
        location_type = row.get("location_type", "") or "0"
        parent_id = row.get("parent_station", "")
        if location_type == "1":
            if stop_id != expected_parent or parent_id:
                raise PipelineError(f"Malformed CZPTT station parent: {stop_id}")
        else:
            if parent_id != expected_parent or parent_id not in by_id:
                raise PipelineError(f"CZPTT boarding stop lacks its station parent: {stop_id}")
            if by_id[parent_id].get("stop_name", "") != row.get("stop_name", ""):
                raise PipelineError(f"CZPTT child stop name differs from its parent: {stop_id}")

        expected_coordinates = sr70.get(primary_code) if country == "CZ" else None
        if expected_coordinates is None:
            continue
        try:
            actual = (float(row.get("stop_lat", "")), float(row.get("stop_lon", "")))
        except ValueError as error:
            raise PipelineError(f"CZPTT stop lacks numeric SR70 coordinates: {stop_id}") from error
        if any(
            abs(left - right) > 0.000001
            for left, right in zip(actual, expected_coordinates, strict=True)
        ):
            raise PipelineError(
                f"CZPTT stop does not carry its SR70 coordinates: {stop_id}; "
                f"expected={expected_coordinates}, actual={actual}"
            )


def _download_sources(
    config: BuildConfig,
    sources: Path,
    timetable_year: int,
    reporter: Reporter,
    clock: StageClock,
) -> tuple[list[SourceRecord], Path]:
    """Download the annual and monthly CZPTT objects until the inventory stops growing."""

    clock.start("discover-source")
    discovery_task = reporter.start("Discover CZPTT source inventory", unit="files")
    inventory = discover_remote_inventory(config.source_base_url, timetable_year)
    reporter.update(
        discovery_task,
        completed=len(inventory),
        detail=f"{len(inventory)} objects",
    )
    reporter.finish(discovery_task, f"{len(inventory)} objects")
    records: list[SourceRecord] = []
    to_download = inventory
    catalog: Path | None = None
    cache = (
        config.source_cache_dir / str(timetable_year)
        if config.source_cache_dir is not None
        else None
    )
    downloader = _HttpSourceDownloader(sources, cache)
    worker_count = config.download_connections
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            for attempt in range(1, MAX_INVENTORY_DOWNLOAD_PASSES + 1):
                clock.start("download-source")
                download_task = reporter.start(
                    f"Download CZPTT sources ({attempt}/{MAX_INVENTORY_DOWNLOAD_PASSES})",
                    total=len(to_download),
                    unit="files",
                )
                cached_before = downloader.cached
                remaining = iter(to_download)
                pending: set[concurrent.futures.Future[SourceRecord]] = set()
                batch_records: list[SourceRecord] = []
                for _ in range(worker_count * 2):
                    item = next(remaining, None)
                    if item is None:
                        break
                    pending.add(executor.submit(downloader.download, item))
                while pending:
                    finished, pending = concurrent.futures.wait(
                        pending,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in finished:
                        record = future.result()
                        records.append(record)
                        batch_records.append(record)
                        reporter.update(
                            download_task,
                            advance=1,
                            detail=record.relative_path,
                        )
                        item = next(remaining, None)
                        if item is not None:
                            pending.add(executor.submit(downloader.download, item))
                reporter.finish(
                    download_task,
                    f"{len(to_download)} files ({downloader.cached - cached_before} cached)",
                )
                for record in batch_records:
                    _validate_object(sources / record.relative_path, record.kind)

                if attempt == 1:
                    clock.start("snapshot-kadr")
                    catalog = snapshot_kadr(sources / "kadr")

                clock.start("recheck-source")
                rediscovery_task = reporter.start("Recheck CZPTT source inventory", unit="files")
                later = discover_remote_inventory(config.source_base_url, timetable_year)
                reporter.update(
                    rediscovery_task,
                    completed=len(later),
                    detail=f"{len(later)} objects",
                )
                reporter.finish(rediscovery_task, f"{len(later)} objects")
                discovered = {value.relative_path for value in inventory}
                rediscovered = {value.relative_path for value in later}
                missing = discovered - rediscovered
                if missing:
                    raise PipelineError(f"Discovered CZPTT objects disappeared: {sorted(missing)}")
                additions = rediscovered - discovered
                if not additions:
                    break
                if attempt == MAX_INVENTORY_DOWNLOAD_PASSES:
                    raise PipelineError(
                        f"CZPTT inventory kept growing after {attempt} download passes; "
                        f"new objects remain: {sorted(additions)}"
                    )
                reporter.note(
                    f"Found {len(additions)} new CZPTT objects; downloading them "
                    f"in pass {attempt + 1}/{MAX_INVENTORY_DOWNLOAD_PASSES}"
                )
                to_download = [value for value in later if value.relative_path in additions]
                inventory = later
    finally:
        downloader.close()
    assert catalog is not None
    if config.source_cache_dir is not None:
        _prune_source_cache(config.source_cache_dir, timetable_year, inventory)
    records.sort(key=lambda value: value.relative_path)
    write_json(
        sources / "inventory.json",
        {
            "schema_version": 1,
            "timetable_year": timetable_year,
            "objects": [asdict(value) for value in inventory],
        },
    )
    write_json(
        sources / "sources.json",
        {
            "schema_version": 1,
            "timetable_year": timetable_year,
            "source_base_url": config.source_base_url,
            "objects": [asdict(value) for value in records],
        },
    )
    return records, catalog


def snapshot_sources(config: BuildConfig, destination: Path, reporter: Reporter) -> Path:
    """Download the CZPTT inventory, KADR catalog and SR70 into a reusable source snapshot.

    The result is what the compiler's ``[czptt] snapshot`` reads: ``sources.json``,
    ``annual/``, ``changes/``, ``kadr/`` and ``sr70/``.
    """

    destination.mkdir(parents=True, exist_ok=False)
    timetable_year = resolve_timetable_year(config.timetable_year)
    _download_sources(config, destination, timetable_year, reporter, StageClock())
    _snapshot_sr70(config, destination)
    _finalize_sources_manifest(destination)
    return destination


def _snapshot_sr70(config: BuildConfig, sources: Path) -> Path:
    destination = sources / "sr70" / "SR70.csv"
    sr70 = config.sr70 or config.geodata_root / "rail" / "SR70.csv"
    if not sr70.is_file():
        raise PipelineError(f"SR70 snapshot does not exist: {sr70}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sr70, destination)
    return destination
