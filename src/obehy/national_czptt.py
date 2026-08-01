"""Build an immutable national CZPTT conversion bundle."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from obehy.national_jdf import (
    BuildReporter,
    CommandFn,
    CommandProgress,
    PipelineError,
    Reporter,
    file_digest,
    git_identity,
    run_command,
    write_json,
)
from obehy.osm_snapshot import (
    OsmSnapshotError,
    validate_railway_locations,
    validate_snapshot,
)
from obehy.runtime_config import ConfigurationError, load_runtime_config

DEFAULT_SOURCE_BASE_URL = "https://portal.cisjr.cz/pub/draha/celostatni/szdc"
KADR_ENDPOINT = "https://provoz.spravazeleznic.cz/kadrws/ciselniky.asmx"
KADR_OPERATIONS = (
    "SeznamSpolecnosti",
    "SeznamDruhuVlaku",
    "SeznamKomercniDruhVlaku",
    "SeznamLinky",
    "SeznamIDS",
)
PARQUET_FILES = {
    "operational_points.parquet",
    "operational_calls.parquet",
    "source_call_metadata.parquet",
    "source_ids_coverage_metadata.parquet",
    "source_ids_coverage_trip_metadata.parquet",
}
OSM_REVIEW_PATH = Path(__file__).with_name("data") / "czptt_osm_aliases.json"
ProgressMode = Literal["auto", "rich", "plain", "off"]
OperationalPointMode = Literal["gtfs", "sidecar"]
JobSetting = Literal["auto"] | int


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
    output: Path
    workdir: Path
    osm_file: Path
    geodata_root: Path
    jrutil_root: Path | None
    jrutil_command: tuple[str, ...] | None
    timetable_year: int | Literal["auto"] = "auto"
    operational_points: OperationalPointMode = "gtfs"
    source_base_url: str = DEFAULT_SOURCE_BASE_URL
    source_snapshot: Path | None = None
    sr70: Path | None = None
    sr70_name20: Path | None = None
    jobs: JobSetting = "auto"
    keep_work: bool = False
    progress: ProgressMode = "auto"


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


def _jobs(value: JobSetting) -> int:
    return 8 if value == "auto" else value


def _second_sunday(year: int) -> date:
    value = date(year, 12, 1)
    return value + timedelta(days=(6 - value.weekday()) % 7 + 7)


def automatic_timetable_year(now: datetime | None = None) -> int:
    """Return the GVD year active in Europe/Prague at *now*."""
    prague = ZoneInfo("Europe/Prague")
    local = (now or datetime.now(prague)).astimezone(prague)
    boundary = _second_sunday(local.year)
    return local.year + 1 if local.date() >= boundary else local.year


def resolve_timetable_year(value: int | Literal["auto"], now: datetime | None = None) -> int:
    return automatic_timetable_year(now) if value == "auto" else value


def _read_url(url: str, *, data: bytes | None = None, headers: Mapping[str, str] = {}) -> bytes:
    request_headers = {"User-Agent": "Obehy/0.1 national-CZPTT builder", **headers}
    request = Request(url, data=data, headers=request_headers)
    with urlopen(request, timeout=120) as response:
        return response.read()


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
    year_names = _listing_names(_read_url(year_url))
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
        for filename in _listing_names(_read_url(month_url)):
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
    """Stream source objects over per-worker persistent HTTP connections."""

    def __init__(self, sources: Path) -> None:
        self.sources = sources
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

        for attempt in range(2):
            connection = self._connection(scheme, parsed.hostname, port)
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "User-Agent": "Obehy/0.1 national-CZPTT builder",
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
                if attempt == 1:
                    raise PipelineError(f"CZPTT download failed: {item.url}: {error}") from error
        raise AssertionError("unreachable HTTP download retry state")

    def close(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for connection in clients:
            connection.close()


def _snapshot_kadr(destination: Path) -> Path:
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
        payload = _read_url(
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
        },
    )
    return catalog


def _validate_object(path: Path, kind: str) -> None:
    if kind == "annual_zip":
        if path.read_bytes()[:4] != b"PK\x03\x04":
            raise PipelineError(f"Annual CZPTT object lacks ZIP magic: {path}")
        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
                if bad is not None:
                    raise PipelineError(f"Corrupt annual CZPTT ZIP member: {bad}")
        except zipfile.BadZipFile as error:
            raise PipelineError(f"Malformed annual CZPTT ZIP: {path}") from error
    elif path.read_bytes()[:2] != b"\x1f\x8b":
        raise PipelineError(f"Monthly CZPTT object lacks gzip magic: {path}")


def _validate_source_manifest(sources: Path) -> list[SourceRecord]:
    manifest_path = sources / "sources.json"
    if not manifest_path.is_file():
        raise PipelineError("Source snapshot is missing sources.json")
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    records = [SourceRecord(**cast(dict[str, Any], value)) for value in manifest["objects"]]
    for record in records:
        path = sources / record.relative_path
        if not path.is_file():
            raise PipelineError(f"Source snapshot object is missing: {record.relative_path}")
        if path.stat().st_size != record.bytes or file_digest(path) != record.sha256:
            raise PipelineError(f"Source snapshot object changed: {record.relative_path}")
        _validate_object(path, record.kind)
    for value in cast(list[dict[str, Any]], manifest.get("auxiliary", [])):
        relative_path = cast(str, value["relative_path"])
        path = sources / relative_path
        if (
            not path.is_file()
            or path.stat().st_size != cast(int, value["bytes"])
            or file_digest(path) != cast(str, value["sha256"])
        ):
            raise PipelineError(f"Source snapshot auxiliary object changed: {relative_path}")
    return records


def _copy_snapshot(snapshot: Path, destination: Path) -> list[SourceRecord]:
    root = snapshot / "sources" if (snapshot / "sources").is_dir() else snapshot
    if not root.is_dir():
        raise PipelineError(f"Source snapshot directory does not exist: {snapshot}")
    shutil.copytree(root, destination, dirs_exist_ok=True)
    return _validate_source_manifest(destination)


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


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class _Message:
    source_path: str
    root_type: str
    identity: str
    payload: bytes


def _message(payload: bytes, source_path: str) -> _Message:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise PipelineError(f"Malformed CZPTT XML in {source_path}: {error}") from error
    root_type = _local_name(root.tag)
    if root_type not in {"CZPTTCISMessage", "CZCanceledPTTMessage"}:
        raise PipelineError(f"Unsupported CZPTT root {root_type!r} in {source_path}")
    identifiers: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) not in {
            "PlannedTransportIdentifiers",
            "RelatedPlannedTransportIdentifiers",
        }:
            continue
        values = {_local_name(child.tag): (child.text or "").strip() for child in element}
        if {"ObjectType", "Company", "Core", "Variant", "TimetableYear"} <= values.keys():
            identifiers.append(
                ":".join(
                    values[name]
                    for name in ("ObjectType", "Company", "Core", "Variant", "TimetableYear")
                )
            )
    identity = "|".join(sorted(identifiers))
    if not identity:
        raise PipelineError(f"CZPTT message has no full transport identity: {source_path}")
    return _Message(source_path, root_type, identity, payload)


def flatten_messages(sources: Path, records: Sequence[SourceRecord], destination: Path) -> int:
    messages: list[_Message] = []
    for record in sorted(records, key=lambda value: value.relative_path):
        path = sources / record.relative_path
        _validate_object(path, record.kind)
        if record.kind == "annual_zip":
            with zipfile.ZipFile(path) as archive:
                for info in sorted(archive.infolist(), key=lambda value: value.filename):
                    if not info.is_dir() and info.filename.casefold().endswith(".xml"):
                        source_path = f"{record.relative_path}//{info.filename}"
                        messages.append(_message(archive.read(info), source_path))
        else:
            try:
                payload = gzip.decompress(path.read_bytes())
            except (OSError, EOFError) as error:
                raise PipelineError(f"Malformed CZPTT gzip: {record.relative_path}") from error
            messages.append(_message(payload, record.relative_path))

    annual = [value for value in messages if value.source_path.startswith("annual/")]
    changes = [value for value in messages if not value.source_path.startswith("annual/")]
    changes.sort(
        key=lambda value: (
            PurePosixPath(value.source_path).parts[:2],
            0 if value.root_type == "CZCanceledPTTMessage" else 1,
            value.source_path,
        )
    )
    ordered = annual + changes
    seen_payloads: set[str] = set()
    timetable_identities: dict[str, str] = {}
    deduplicated: list[_Message] = []
    for value in ordered:
        digest = hashlib.sha256(value.payload).hexdigest()
        if digest in seen_payloads:
            continue
        seen_payloads.add(digest)
        if value.root_type == "CZPTTCISMessage":
            previous = timetable_identities.setdefault(value.identity, digest)
            if previous != digest:
                raise PipelineError(
                    f"Conflicting CZPTT timetable messages share identity {value.identity}"
                )
        deduplicated.append(value)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for index, value in enumerate(deduplicated):
            info = zipfile.ZipInfo(f"{index:09d}.xml", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, value.payload)
    return len(deduplicated)


def _multitool_dll(root: Path) -> Path:
    return root / "jrutil-multitool" / "bin" / "Release" / "net10.0" / "jrutil-multitool.dll"


def _build_command(root: Path) -> list[str]:
    project = root / "jrutil-multitool" / "jrutil-multitool.fsproj"
    return ["dotnet", "build", str(project), "-c", "Release", "--no-restore"]


def _jrutil_cwd(config: BuildConfig) -> Path:
    return config.jrutil_root or config.workdir


def _jrutil_provenance(config: BuildConfig) -> dict[str, object]:
    if config.jrutil_root is not None:
        return {
            "mode": "directory",
            "directory": str(config.jrutil_root.resolve()),
            "git": git_identity(config.jrutil_root),
        }
    assert config.jrutil_command is not None
    files: list[dict[str, object]] = []
    for argument in config.jrutil_command:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.is_file():
            files.append(
                {
                    "path": str(candidate.resolve()),
                    "bytes": candidate.stat().st_size,
                    "sha256": file_digest(candidate),
                }
            )
    return {"mode": "command", "command": list(config.jrutil_command), "files": files}


def _converter_command(
    config: BuildConfig,
    messages: Path,
    catalog: Path,
    bundle: Path,
) -> list[str]:
    runtime = (
        list(config.jrutil_command)
        if config.jrutil_command is not None
        else ["dotnet", str(_multitool_dll(cast(Path, config.jrutil_root)))]
    )
    return [
        *runtime,
        "czptt-to-bundle",
        "--progress-events",
        f"--catalog-snapshot={catalog}",
        f"--operational-points={config.operational_points}",
        f"--sr70={messages.parent.parent / 'sources' / 'sr70' / 'SR70.csv'}",
        f"--sr70-name20={messages.parent.parent / 'sources' / 'sr70' / 'SR70_Nazev20.csv'}",
        f"--osm-pbf={config.osm_file}",
        f"--osm-aliases={OSM_REVIEW_PATH}",
        str(messages),
        str(bundle),
    ]


def _manifest(root: Path) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        entries.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": file_digest(path)}
        )
    return {"schema_version": 1, "files": entries}


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


def _verify_gtfs_stops(gtfs: Path, sr70_path: Path) -> None:
    stops_path = gtfs / "stops.txt"
    if not stops_path.is_file():
        raise PipelineError("JrUtil CZPTT bundle is missing gtfs-intermediate/stops.txt")
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


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise PipelineError(f"CZPTT bundle is missing {path.name}: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _stop_time_keys(path: Path, required: set[tuple[str, str]]) -> set[tuple[str, str]]:
    if not path.is_file():
        raise PipelineError(f"CZPTT bundle is missing {path.name}: {path}")
    missing = set(required)
    if not missing:
        return missing
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            missing.discard((row.get("trip_id", ""), row.get("stop_sequence", "")))
            if not missing:
                break
    return missing


def _verify_extensions(bundle: Path) -> None:
    gtfs = bundle / "gtfs-intermediate"
    extensions = bundle / "extensions"
    extension_files = ("cz_routes.txt", "cz_trips.txt", "cz_trip_stop_zones.txt")
    if not extensions.is_dir():
        raise PipelineError("JrUtil CZPTT bundle is missing extensions/")
    misplaced = [name for name in extension_files if (gtfs / name).exists()]
    if misplaced:
        raise PipelineError(f"CZPTT extensions leaked into standard GTFS: {misplaced}")
    missing = [name for name in extension_files if not (extensions / name).is_file()]
    if missing:
        raise PipelineError(f"JrUtil CZPTT bundle is missing extensions: {missing}")

    route_ids = {row["route_id"] for row in _csv_rows(gtfs / "routes.txt")}
    trip_ids = {row["trip_id"] for row in _csv_rows(gtfs / "trips.txt")}
    for row in _csv_rows(extensions / "cz_routes.txt"):
        if row.get("route_id") not in route_ids:
            raise PipelineError(f"cz_routes.txt references an unknown route: {row}")
    for row in _csv_rows(extensions / "cz_trips.txt"):
        if row.get("trip_id") not in trip_ids:
            raise PipelineError(f"cz_trips.txt references an unknown trip: {row}")
    zone_rows = _csv_rows(extensions / "cz_trip_stop_zones.txt")
    required_stop_times = {
        (row.get("trip_id", ""), row.get("stop_sequence", "")) for row in zone_rows
    }
    missing_stop_times = _stop_time_keys(gtfs / "stop_times.txt", required_stop_times)
    if missing_stop_times:
        example = next(
            row
            for row in zone_rows
            if (row.get("trip_id", ""), row.get("stop_sequence", "")) in missing_stop_times
        )
        raise PipelineError(f"cz_trip_stop_zones.txt references an unknown stop time: {example}")


def _verify_foreign_coordinate_acceptance(bundle: Path) -> None:
    diagnostics = cast(
        dict[str, object],
        json.loads((bundle / "diagnostics.json").read_text(encoding="utf-8")),
    )
    coordinate_value = diagnostics.get("coordinate_diagnostics", {})
    if not isinstance(coordinate_value, dict):
        raise PipelineError("CZPTT diagnostics lack coordinate_diagnostics")
    coordinate = cast(dict[str, object], coordinate_value)
    unresolved = coordinate.get(
        "unresolvedPassengerPointIds",
        coordinate.get("unresolvedPointIds", []),
    )
    unresolved_values = cast(list[object], unresolved) if isinstance(unresolved, list) else []
    if not isinstance(unresolved, list) or any(
        not isinstance(value, str) for value in unresolved_values
    ):
        raise PipelineError("CZPTT unresolvedPointIds diagnostics are malformed")
    foreign = sorted(
        value
        for value in cast(list[str], unresolved_values)
        if not value.startswith("czptt:stop:CZ:")
    )
    review = cast(
        dict[str, object],
        json.loads(OSM_REVIEW_PATH.read_text(encoding="utf-8")),
    )
    dispositions_value = review.get("residual_dispositions", {})
    if not isinstance(dispositions_value, dict):
        raise PipelineError("CZPTT OSM residual_dispositions must be an object")
    dispositions = cast(dict[str, object], dispositions_value)
    missing = [value for value in foreign if not isinstance(dispositions.get(value), str)]
    if missing:
        raise PipelineError(
            "Passenger-referenced foreign CZPTT locations lack coordinates or a reviewed "
            f"residual disposition: {missing}"
        )


def _validate_config(config: BuildConfig) -> None:
    if config.output.exists():
        raise PipelineError(f"Output path must not exist: {config.output}")
    if (config.jrutil_root is None) == (config.jrutil_command is None):
        raise PipelineError("Exactly one JrUtil runtime mode must be configured")
    for label, path in (
        ("workdir", config.workdir),
        ("osm_file", config.osm_file),
        ("geodata_root", config.geodata_root),
    ):
        if not path.is_absolute():
            raise PipelineError(f"{label} must be an absolute path: {path}")
    if config.jrutil_root is not None and not config.jrutil_root.is_dir():
        raise PipelineError(f"JrUtil root does not exist: {config.jrutil_root}")
    if not config.geodata_root.is_dir():
        raise PipelineError(f"Geodata root does not exist: {config.geodata_root}")
    if config.source_snapshot is not None and config.source_base_url != DEFAULT_SOURCE_BASE_URL:
        raise PipelineError("--source-snapshot forbids --source-base-url")
    if config.source_snapshot is not None and (
        config.sr70 is not None or config.sr70_name20 is not None
    ):
        raise PipelineError("--source-snapshot forbids SR70 overrides")
    if config.sr70_name20 is not None and config.sr70 is None:
        raise PipelineError("--sr70-name20 requires --sr70")
    if _jobs(config.jobs) <= 0:
        raise PipelineError("--jobs must be auto or a positive integer")


def _resolve_osm_snapshot(config: BuildConfig) -> tuple[Path, dict[str, Any]]:
    return config.osm_file, validate_snapshot(config.osm_file, config.workdir)


def build(
    config: BuildConfig,
    *,
    command_runner: CommandFn = run_command,
    reporter: Reporter | None = None,
) -> Path:
    _validate_config(config)
    _osm_file, osm_manifest = _resolve_osm_snapshot(config)
    output = config.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.parent / f".{output.name}.work-{uuid.uuid4().hex}"
    publish = stage / "publish"
    sources = publish / "sources"
    derived = publish / "derived"
    logs = stage / "logs"
    run_root = (
        config.workdir.resolve()
        / "runs"
        / "national-czptt"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}"
    )
    work = run_root / "work"
    for directory in (publish, sources, derived, logs, work):
        directory.mkdir(parents=True, exist_ok=True)
    own_reporter = reporter is None
    reporter = reporter or BuildReporter(config.progress)
    timetable_year = resolve_timetable_year(config.timetable_year)
    active_stage = "initialization"
    try:
        reporter.note(
            f"CZPTT GVD {timetable_year}; operational points={config.operational_points}; "
            f"workers={_jobs(config.jobs)}; run={run_root}; staging={stage}"
        )
        if config.source_snapshot is not None:
            active_stage = "copy-source-snapshot"
            records = _copy_snapshot(config.source_snapshot, sources)
            catalog = sources / "kadr" / "catalog.json"
            if not catalog.is_file():
                raise PipelineError("Source snapshot is missing kadr/catalog.json")
        else:
            active_stage = "discover-source"
            discovery_task = reporter.start("Discover CZPTT source inventory", unit="files")
            inventory = discover_remote_inventory(config.source_base_url, timetable_year)
            reporter.update(
                discovery_task,
                completed=len(inventory),
                detail=f"{len(inventory)} objects",
            )
            reporter.finish(discovery_task, f"{len(inventory)} objects")
            write_json(
                sources / "inventory.json",
                {
                    "schema_version": 1,
                    "timetable_year": timetable_year,
                    "objects": [asdict(value) for value in inventory],
                },
            )
            active_stage = "download-source"
            download_task = reporter.start(
                "Download CZPTT sources",
                total=len(inventory),
                unit="files",
            )
            downloader = _HttpSourceDownloader(sources)
            worker_count = _jobs(config.jobs)
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                    remaining = iter(inventory)
                    pending: set[concurrent.futures.Future[SourceRecord]] = set()
                    for _ in range(worker_count * 2):
                        item = next(remaining, None)
                        if item is None:
                            break
                        pending.add(executor.submit(downloader.download, item))
                    records: list[SourceRecord] = []
                    while pending:
                        finished, pending = concurrent.futures.wait(
                            pending,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )
                        for future in finished:
                            record = future.result()
                            records.append(record)
                            reporter.update(
                                download_task,
                                advance=1,
                                detail=record.relative_path,
                            )
                            item = next(remaining, None)
                            if item is not None:
                                pending.add(executor.submit(downloader.download, item))
            finally:
                downloader.close()
            reporter.finish(download_task, f"{len(records)} files")
            records.sort(key=lambda value: value.relative_path)
            for record in records:
                _validate_object(sources / record.relative_path, record.kind)
            active_stage = "snapshot-kadr"
            catalog = _snapshot_kadr(sources / "kadr")
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
            if additions:
                reporter.problem(
                    "warning",
                    f"{len(additions)} CZPTT objects appeared after inventory freeze "
                    "and were ignored",
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

        active_stage = "snapshot-sr70"
        sr70_destination = sources / "sr70" / "SR70.csv"
        sr70_name20_destination = sources / "sr70" / "SR70_Nazev20.csv"
        if not sr70_destination.exists() and not sr70_name20_destination.exists():
            default_sr70 = config.geodata_root / "rail" / "SR70.csv"
            sr70 = config.sr70 or default_sr70
            sr70_name20 = config.sr70_name20 or sr70.with_name("SR70_Nazev20.csv")
            if not sr70.is_file():
                raise PipelineError(f"SR70 snapshot does not exist: {sr70}")
            if not sr70_name20.is_file():
                raise PipelineError(
                    f"SR70 Název20 companion snapshot does not exist: {sr70_name20}"
                )
            sr70_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sr70, sr70_destination)
            shutil.copy2(sr70_name20, sr70_name20_destination)
        elif not sr70_destination.is_file() or not sr70_name20_destination.is_file():
            raise PipelineError(
                "SR70 source snapshot must contain both sr70/SR70.csv and sr70/SR70_Nazev20.csv"
            )
        _finalize_sources_manifest(sources)

        active_stage = "flatten-messages"
        message_count = flatten_messages(sources, records, derived / "messages.zip")

        active_stage = "build-jrutil"
        if config.jrutil_root is not None:
            command_runner(
                _build_command(config.jrutil_root),
                config.jrutil_root,
                logs / "jrutil-build.process.log",
                reporter,
                CommandProgress("Build JrUtil", stage=active_stage),
            )
        active_stage = "validate-osm-railway-locations"
        filtered_osm = validate_railway_locations(
            config.workdir,
            str(osm_manifest["merge_key"]),
        )
        converter_config = replace(config, osm_file=filtered_osm)
        active_stage = "convert"
        bundle = publish / "bundle"
        command_runner(
            _converter_command(converter_config, derived / "messages.zip", catalog, bundle),
            _jrutil_cwd(config),
            logs / "jrutil-czptt.process.log",
            reporter,
            CommandProgress("Convert CZPTT", stage=active_stage),
        )
        required = PARQUET_FILES | {"diagnostics.json", "manifest.json"}
        missing_outputs = sorted(name for name in required if not (bundle / name).is_file())
        if missing_outputs or not (bundle / "gtfs-intermediate").is_dir():
            raise PipelineError(f"JrUtil CZPTT bundle is incomplete: {missing_outputs}")
        active_stage = "verify-bundle"
        _verify_gtfs_stops(bundle / "gtfs-intermediate", sr70_destination)
        _verify_extensions(bundle)
        _verify_foreign_coordinate_acceptance(bundle)

        active_stage = "run-manifest"
        run_manifest = {
            "schema_version": 1,
            "pipeline": "obehy-national-czptt",
            "timetable_year": timetable_year,
            "operational_points": config.operational_points,
            "message_count": message_count,
            "jrutil": _jrutil_provenance(config),
            "osm_source_key": osm_manifest["merge_key"],
            "sources_manifest_sha256": file_digest(sources / "sources.json"),
            "messages_sha256": file_digest(derived / "messages.zip"),
            "sr70_sha256": file_digest(sr70_destination),
            "sr70_name20_sha256": file_digest(sr70_name20_destination),
        }
        write_json(publish / "run-manifest.json", run_manifest)
        if config.keep_work:
            shutil.copytree(logs, work / "logs", dirs_exist_ok=True)
            shutil.copytree(work, publish / "work", dirs_exist_ok=True)
        write_json(publish / "manifest.json", _manifest(publish))
        os.replace(publish, output)
        shutil.rmtree(stage)
        if not config.keep_work:
            shutil.rmtree(run_root)
        reporter.note(f"National CZPTT bundle written to {output}")
        return output
    except Exception as error:
        write_json(
            stage / "failure.json",
            {
                "schema_version": 1,
                "stage": active_stage,
                "error_type": type(error).__name__,
                "message": str(error),
                "staging_directory": str(stage),
                "run_directory": str(run_root),
            },
        )
        reporter.note(f"FAILED STAGING RETAINED: {stage}")
        raise
    finally:
        if own_reporter:
            reporter.close()


def _parse_year(value: str) -> int | Literal["auto"]:
    if value.casefold() == "auto":
        return "auto"
    try:
        year = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be auto or a four-digit year") from error
    if not 2000 <= year <= 9999:
        raise argparse.ArgumentTypeError("must be auto or a four-digit year")
    return year


def _parse_jobs(value: str) -> JobSetting:
    if value.casefold() == "auto":
        return "auto"
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be auto or a positive integer") from error
    if count <= 0:
        raise argparse.ArgumentTypeError("must be auto or a positive integer")
    return count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obehy-national-czptt")
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="build a national CZPTT bundle")
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--config", type=Path)
    build_parser.add_argument("--timetable-year", type=_parse_year, default="auto")
    build_parser.add_argument("--operational-points", choices=("gtfs", "sidecar"), default="gtfs")
    build_parser.add_argument("--source-base-url", default=DEFAULT_SOURCE_BASE_URL)
    build_parser.add_argument("--source-snapshot", type=Path)
    build_parser.add_argument("--sr70", type=Path)
    build_parser.add_argument("--sr70-name20", type=Path)
    build_parser.add_argument("--jobs", type=_parse_jobs, default="auto")
    build_parser.add_argument("--keep-work", action="store_true")
    build_parser.add_argument(
        "--progress", choices=("auto", "rich", "plain", "off"), default="auto"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime = load_runtime_config(cast(Path | None, args.config))
        config = BuildConfig(
            output=cast(Path, args.output),
            workdir=runtime.workdir,
            osm_file=runtime.osm_file,
            geodata_root=runtime.jrunify_ext_geodata_dir,
            jrutil_root=runtime.jrutil.directory,
            jrutil_command=runtime.jrutil.command,
            timetable_year=cast(int | Literal["auto"], args.timetable_year),
            operational_points=cast(OperationalPointMode, args.operational_points),
            source_base_url=cast(str, args.source_base_url),
            source_snapshot=cast(Path | None, args.source_snapshot),
            sr70=cast(Path | None, args.sr70),
            sr70_name20=cast(Path | None, args.sr70_name20),
            jobs=cast(JobSetting, args.jobs),
            keep_work=cast(bool, args.keep_work),
            progress=cast(ProgressMode, args.progress),
        )
        result = build(config)
    except (
        ConfigurationError,
        OsmSnapshotError,
        OSError,
        PipelineError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"National CZPTT bundle written to {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
