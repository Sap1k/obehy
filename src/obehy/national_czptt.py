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
import tempfile
import threading
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from obehy.gvd import resolve_timetable_year
from obehy.osm_snapshot import (
    OsmSnapshotError,
    validate_railway_locations,
    validate_snapshot,
)
from obehy.pipeline import jrutil
from obehy.pipeline.args import JobSetting, parse_jobs, parse_memory_budget, parse_year
from obehy.pipeline.download import USER_AGENT, read_url
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import atomic_output_path, file_digest, write_json
from obehy.pipeline.process import (
    CommandFn,
    CommandResult,
    command_manifest,
    failure_record,
    report_failure,
    run_command,
)
from obehy.pipeline.reporting import BuildReporter, CommandProgress, Reporter, StageClock
from obehy.pipeline.staging import create as create_staging
from obehy.production_package import ProductionPackageError, extracted_gtfs, read_manifest
from obehy.runtime_config import ConfigurationError, load_runtime_config

DEFAULT_SOURCE_BASE_URL = "https://portal.cisjr.cz/pub/draha/celostatni/szdc"
MAX_INVENTORY_DOWNLOAD_PASSES = 5
KADR_ENDPOINT = "https://provoz.spravazeleznic.cz/kadrws/ciselniky.asmx"
KADR_OPERATIONS = (
    "SeznamSpolecnosti",
    "SeznamDruhuVlaku",
    "SeznamKomercniDruhVlaku",
    "SeznamLinky",
    "SeznamIDS",
    "SeznamPoznamkyKJR",
)
OSM_REVIEW_PATH = Path(__file__).with_name("data") / "czptt_osm_aliases.json"
ProgressMode = Literal["auto", "rich", "plain", "off"]
OperationalPointMode = Literal["gtfs", "sidecar"]


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
    operational_points: OperationalPointMode = "sidecar"
    source_base_url: str = DEFAULT_SOURCE_BASE_URL
    source_snapshot: Path | None = None
    sr70: Path | None = None
    jobs: JobSetting = "auto"
    memory_budget: str = "auto"
    keep_work: bool = False
    progress: ProgressMode = "auto"
    build_jrutil: bool = True


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
    event_time: datetime
    payload: bytes


@dataclass(frozen=True)
class _SpooledMessage:
    source_path: str
    root_type: str
    identity: str
    event_time: datetime
    payload_path: Path
    sha256: str


def _message(payload: bytes, source_path: str) -> _Message:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise PipelineError(f"Malformed CZPTT XML in {source_path}: {error}") from error
    root_type = _local_name(root.tag)
    if root_type not in {"CZPTTCISMessage", "CZCanceledPTTMessage"}:
        raise PipelineError(f"Unsupported CZPTT root {root_type!r} in {source_path}")
    timestamp_name = "CZPTTCreation" if root_type == "CZPTTCISMessage" else "CZPTTCancelation"
    timestamp_text = next(
        ((child.text or "").strip() for child in root if _local_name(child.tag) == timestamp_name),
        "",
    )
    if not timestamp_text:
        raise PipelineError(f"CZPTT message has no {timestamp_name}: {source_path}")
    try:
        event_time = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
    except ValueError as error:
        raise PipelineError(
            f"CZPTT message has invalid {timestamp_name} {timestamp_text!r}: {source_path}"
        ) from error
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=ZoneInfo("Europe/Prague"))
    event_time = event_time.astimezone(UTC)
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
    return _Message(source_path, root_type, identity, event_time, payload)


def flatten_messages(sources: Path, records: Sequence[SourceRecord], destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    spool = Path(tempfile.mkdtemp(prefix=".czptt-messages-", dir=destination.parent))
    messages: list[_SpooledMessage] = []

    def spool_message(source: Any, source_path: str) -> None:
        payload_path = spool / f"{len(messages):09d}.xml"
        digest = hashlib.sha256()
        with payload_path.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        parsed = _message(payload_path.read_bytes(), source_path)
        messages.append(
            _SpooledMessage(
                source_path=parsed.source_path,
                root_type=parsed.root_type,
                identity=parsed.identity,
                event_time=parsed.event_time,
                payload_path=payload_path,
                sha256=digest.hexdigest(),
            )
        )

    try:
        for record in sorted(records, key=lambda value: value.relative_path):
            path = sources / record.relative_path
            _validate_object(path, record.kind)
            if record.kind == "annual_zip":
                with zipfile.ZipFile(path) as archive:
                    for info in sorted(archive.infolist(), key=lambda value: value.filename):
                        if not info.is_dir() and info.filename.casefold().endswith(".xml"):
                            with archive.open(info) as source:
                                spool_message(source, f"{record.relative_path}//{info.filename}")
            else:
                try:
                    with gzip.open(path, "rb") as source:
                        spool_message(source, record.relative_path)
                except (OSError, EOFError) as error:
                    raise PipelineError(f"Malformed CZPTT gzip: {record.relative_path}") from error

        annual = [value for value in messages if value.source_path.startswith("annual/")]
        changes = [value for value in messages if not value.source_path.startswith("annual/")]
        changes.sort(
            key=lambda value: (
                value.event_time,
                0 if value.root_type == "CZPTTCISMessage" else 1,
                value.source_path,
            )
        )
        ordered = annual + changes
        seen_payloads: set[str] = set()
        timetable_identities: dict[str, str] = {}
        deduplicated: list[_SpooledMessage] = []
        for value in ordered:
            if value.sha256 in seen_payloads:
                continue
            seen_payloads.add(value.sha256)
            if value.root_type == "CZPTTCISMessage":
                previous = timetable_identities.setdefault(value.identity, value.sha256)
                if previous != value.sha256:
                    raise PipelineError(
                        f"Conflicting CZPTT timetable messages share identity {value.identity}"
                    )
            deduplicated.append(value)

        with (
            atomic_output_path(destination) as temporary_destination,
            zipfile.ZipFile(
                temporary_destination,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive,
        ):
            for index, value in enumerate(deduplicated):
                info = zipfile.ZipInfo(f"{index:09d}.xml", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                info.create_system = 3
                # writestr preserves the existing deterministic ZIP encoding;
                # only the largest individual XML payload is resident here.
                archive.writestr(info, value.payload_path.read_bytes())
        return len(deduplicated)
    finally:
        shutil.rmtree(spool, ignore_errors=True)


def _jrutil_cwd(config: BuildConfig) -> Path:
    return config.jrutil_root or config.workdir


def _runtime_command(config: BuildConfig) -> list[str]:
    return jrutil.runtime_command(config.jrutil_root, config.jrutil_command)


def _converter_command(
    config: BuildConfig,
    messages: Path,
    catalog: Path,
    bundle: Path,
) -> list[str]:
    return [
        *_runtime_command(config),
        "czptt-to-bundle",
        "--progress-events",
        f"--jobs={config.jobs}",
        f"--memory-budget={config.memory_budget}",
        f"--catalog-snapshot={catalog}",
        f"--operational-points={config.operational_points}",
        f"--sr70={messages.parent.parent / 'sources' / 'sr70' / 'SR70.csv'}",
        f"--osm-pbf={config.osm_file}",
        f"--osm-aliases={OSM_REVIEW_PATH}",
        f"--diagnostics-out={bundle.parent / 'diagnostics-detail'}",
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


def _verify_foreign_coordinate_acceptance(bundle: Path) -> None:
    diagnostics_path = bundle / "diagnostics.json"
    if not diagnostics_path.is_file():
        diagnostics_path = bundle / "events" / "diagnostics.json"
    diagnostics = cast(
        dict[str, object],
        json.loads(diagnostics_path.read_text(encoding="utf-8")),
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
    if config.source_snapshot is not None and config.sr70 is not None:
        raise PipelineError("--source-snapshot forbids SR70 overrides")
    if _jobs(config.jobs) <= 0:
        raise PipelineError("--jobs must be auto or a positive integer")


def _resolve_osm_snapshot(config: BuildConfig) -> tuple[Path, dict[str, Any]]:
    return config.osm_file, validate_snapshot(config.osm_file, config.workdir)


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
    downloader = _HttpSourceDownloader(sources)
    worker_count = _jobs(config.jobs)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            for attempt in range(1, MAX_INVENTORY_DOWNLOAD_PASSES + 1):
                clock.start("download-source")
                download_task = reporter.start(
                    f"Download CZPTT sources ({attempt}/{MAX_INVENTORY_DOWNLOAD_PASSES})",
                    total=len(to_download),
                    unit="files",
                )
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
                reporter.finish(download_task, f"{len(to_download)} files")
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


def _snapshot_sr70(config: BuildConfig, sources: Path) -> Path:
    destination = sources / "sr70" / "SR70.csv"
    if not destination.is_file():
        if config.source_snapshot is not None:
            raise PipelineError("SR70 source snapshot must contain sr70/SR70.csv")
        sr70 = config.sr70 or config.geodata_root / "rail" / "SR70.csv"
        if not sr70.is_file():
            raise PipelineError(f"SR70 snapshot does not exist: {sr70}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sr70, destination)
    return destination


def build(
    config: BuildConfig,
    *,
    command_runner: CommandFn = run_command,
    reporter: Reporter | None = None,
) -> Path:
    _validate_config(config)
    _osm_file, osm_manifest = _resolve_osm_snapshot(config)
    staging = create_staging(config.output, config.workdir, "national-czptt")
    publish = staging.publish
    sources = publish / "sources"
    derived = publish / "derived"
    logs = staging.stage / "logs"
    for directory in (sources, derived, logs):
        directory.mkdir(parents=True)
    own_reporter = reporter is None
    reporter = reporter or BuildReporter(config.progress)
    timetable_year = resolve_timetable_year(config.timetable_year)
    clock = StageClock()
    command_results: dict[str, CommandResult | None] = {}

    try:
        reporter.note(
            f"CZPTT GVD {timetable_year}; operational points={config.operational_points}; "
            f"workers={_jobs(config.jobs)}; run={staging.run_root}; staging={staging.stage}"
        )
        if config.source_snapshot is not None:
            clock.start("copy-source-snapshot")
            records = _copy_snapshot(config.source_snapshot, sources)
            catalog = sources / "kadr" / "catalog.json"
            if not catalog.is_file():
                raise PipelineError("Source snapshot is missing kadr/catalog.json")
        else:
            records, catalog = _download_sources(config, sources, timetable_year, reporter, clock)

        clock.start("snapshot-sr70")
        sr70_destination = _snapshot_sr70(config, sources)
        _finalize_sources_manifest(sources)

        clock.start("flatten-messages")
        message_count = flatten_messages(sources, records, derived / "messages.zip")

        clock.start("build-jrutil")
        if config.build_jrutil and config.jrutil_root is not None:
            command_results["build_jrutil"] = command_runner(
                jrutil.build_command(config.jrutil_root),
                config.jrutil_root,
                logs / "jrutil-build.process.log",
                reporter,
                CommandProgress("Build JrUtil", stage=clock.current),
            )
        clock.start("validate-osm-railway-locations")
        filtered_osm = validate_railway_locations(
            config.workdir,
            str(osm_manifest["merge_key"]),
        )
        converter_config = replace(config, osm_file=filtered_osm)
        clock.start("convert")
        bundle = publish / "bundle"
        command_results["convert"] = command_runner(
            _converter_command(converter_config, derived / "messages.zip", catalog, bundle),
            _jrutil_cwd(config),
            logs / "jrutil-czptt.process.log",
            reporter,
            CommandProgress("Convert CZPTT", stage=clock.current),
        )
        read_manifest(bundle)
        clock.start("verify-bundle")
        with extracted_gtfs(bundle) as gtfs:
            _verify_gtfs_stops(gtfs, sr70_destination)
        _verify_foreign_coordinate_acceptance(publish / "diagnostics-detail")
        command_results["validate_package"] = command_runner(
            [*_runtime_command(converter_config), "validate-package", str(bundle)],
            _jrutil_cwd(config),
            logs / "validate-package.process.log",
            reporter,
            CommandProgress("Validate production package", stage="validate-package"),
        )

        clock.start("run-manifest")
        run_manifest = {
            "schema_version": 1,
            "pipeline": "obehy-national-czptt",
            "timetable_year": timetable_year,
            "operational_points": config.operational_points,
            "message_count": message_count,
            "jrutil": jrutil.provenance(config.jrutil_root, config.jrutil_command),
            "osm_source_key": osm_manifest["merge_key"],
            "sources_manifest_sha256": file_digest(sources / "sources.json"),
            "messages_sha256": file_digest(derived / "messages.zip"),
            "sr70_sha256": file_digest(sr70_destination),
            "execution": {
                "commands": {
                    name: command_manifest(result)
                    for name, result in sorted(command_results.items())
                }
            },
        }
        write_json(publish / "run-manifest.json", run_manifest)
        if config.keep_work:
            shutil.copytree(logs, staging.work / "logs", dirs_exist_ok=True)
            shutil.copytree(staging.work, publish / "work", dirs_exist_ok=True)
        write_json(publish / "manifest.json", _manifest(publish))
        os.replace(publish, staging.output)
        shutil.rmtree(staging.stage)
        if not config.keep_work:
            shutil.rmtree(staging.run_root)
        reporter.note(f"National CZPTT bundle written to {staging.output}")
        return staging.output
    except Exception as error:
        write_json(
            staging.failure_path,
            failure_record(
                error,
                clock,
                reporter,
                staging_directory=str(staging.stage),
                run_directory=str(staging.run_root),
                logs_directory=str(logs),
            ),
        )
        report_failure(reporter, error, clock.current, staging.stage, staging.failure_path)
        raise
    finally:
        if own_reporter:
            reporter.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obehy-national-czptt")
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="build a national CZPTT bundle")
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--config", type=Path)
    build_parser.add_argument("--timetable-year", type=parse_year, default="auto")
    build_parser.add_argument(
        "--operational-points", choices=("gtfs", "sidecar"), default="sidecar"
    )
    build_parser.add_argument("--source-base-url", default=DEFAULT_SOURCE_BASE_URL)
    build_parser.add_argument("--source-snapshot", type=Path)
    build_parser.add_argument("--sr70", type=Path)
    build_parser.add_argument("--jobs", type=parse_jobs, default="auto")
    build_parser.add_argument("--memory-budget", type=parse_memory_budget, default="auto")
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
            jobs=cast(JobSetting, args.jobs),
            memory_budget=cast(str, args.memory_budget),
            keep_work=cast(bool, args.keep_work),
            progress=cast(ProgressMode, args.progress),
        )
        result = build(config)
    except (
        ConfigurationError,
        OsmSnapshotError,
        OSError,
        PipelineError,
        ProductionPackageError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"National CZPTT bundle written to {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
