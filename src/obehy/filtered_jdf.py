"""Filtered national JDF GTFS for consumers that merge it with regional feeds.

The filter mirrors https://github.com/0xaa55h/gtfs-processor: it drops CIS lines that other feeds
already publish (listed by portal.radekpapez.cz plus fixed line-number prefixes) and drops calls at
stops without coordinates. Customs (JDF `$`) stops are already non-boardable in JrUtil's output.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from urllib.parse import urlencode

from obehy.national_jdf import verify_gtfs_stops
from obehy.pipeline.download import read_url
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import deterministic_zip, file_digest, utc_now, write_json
from obehy.production_package import ProductionPackageError, extracted_gtfs

PORTAL_URL = "https://portal.radekpapez.cz/"
RULES = Path(__file__).with_name("data") / "filtered-jdf" / "rules-v1.json"

PostFn = Callable[[str, bytes], bytes]


@dataclass(frozen=True)
class PortalLine:
    line: str
    ids: str
    name: str
    operator: str


@dataclass(frozen=True)
class FilterRules:
    operators: tuple[str, ...]
    ids_codes: tuple[int, ...]
    line_prefixes: tuple[str, ...]


def load_rules(path: Path = RULES) -> FilterRules:
    value = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    prefixes = tuple(str(prefix) for prefix in value["line_prefixes"])
    if any(not prefix.isdigit() for prefix in prefixes):
        raise PipelineError(f"Filtered-JDF line prefixes must be digits: {path}")
    return FilterRules(
        operators=tuple(str(operator) for operator in value["operators"]),
        ids_codes=tuple(int(code) for code in value["ids_codes"]),
        line_prefixes=prefixes,
    )


def normalize_line(value: str) -> str:
    """Return a six-digit CIS line number; the portal prints it without leading zeros."""

    text = value.strip()
    if not text.isdigit() or len(text) > 6:
        raise ValueError(f"Not a CIS line number: {value!r}")
    return text.zfill(6)


class _ResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._table_depth or dict(attrs).get("id") == "vysledky":
                self._table_depth += 1
        elif self._table_depth and tag == "tr":
            self._row = []
        elif self._table_depth and tag == "td" and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if not self._table_depth:
            return
        if tag == "table":
            self._table_depth -= 1
        elif tag == "td" and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_portal_results(html: str) -> list[PortalLine]:
    parser = _ResultsParser()
    parser.feed(html)
    lines: list[PortalLine] = []
    for cells in parser.rows:
        if len(cells) < 3:
            continue
        try:
            line = normalize_line(cells[0])
        except ValueError:
            continue
        operator = cells[3] if len(cells) > 3 else ""
        lines.append(PortalLine(line=line, ids=cells[1], name=cells[2], operator=operator))
    return lines


def _form(
    reference: date, *, operators: Iterable[str] = (), ids_code: int | None = None
) -> list[tuple[str, str]]:
    day = reference.isoformat()
    fields: list[tuple[str, str]] = [("cislo", "")]
    if ids_code is not None:
        fields += [("check_koddopravy", "on"), ("koddopravy", str(ids_code))]
    else:
        fields.append(("koddopravy", ""))
    fields += [("cisloids", ""), ("nazev", ""), ("check_dopravce_ano", "on")]
    selected = list(operators)
    fields += [("dopravci_ano[]", operator) for operator in selected or [""] * 5]
    fields += [("dopravci_ne[]", "")] * 3
    fields += [("zastavky[]", "")] * 5
    fields += [
        ("rezim", "AND"),
        ("datum1", day),
        ("datum2", day),
        ("datum_od", day),
        ("datum_do", day),
        ("vyluka", "vse"),
        ("hledani", "Hledat"),
    ]
    return fields


def _post(url: str, body: bytes) -> bytes:
    return read_url(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})


def fetch_line_snapshot(
    rules: FilterRules,
    reference: date,
    destination: Path,
    *,
    post: PostFn = _post,
) -> Path:
    """Query the portal once per rule group and store raw responses plus parsed lines."""

    destination.mkdir(parents=True, exist_ok=True)
    queries: list[tuple[str, str, list[tuple[str, str]]]] = []
    if rules.operators:
        queries.append(
            ("operators", ";".join(rules.operators), _form(reference, operators=rules.operators))
        )
    for code in rules.ids_codes:
        queries.append(("ids", str(code), _form(reference, ids_code=code)))
    records: list[dict[str, Any]] = []
    for index, (kind, value, fields) in enumerate(queries):
        body = urlencode(fields).encode("ascii")
        payload = post(PORTAL_URL, body)
        raw = destination / f"query-{index:02d}-{kind}.html"
        raw.write_bytes(payload)
        lines = parse_portal_results(payload.decode("utf-8", errors="replace"))
        if not lines:
            raise PipelineError(f"Line portal returned no lines for {kind}={value}")
        records.append(
            {
                "kind": kind,
                "value": value,
                "response_file": raw.name,
                "response_sha256": hashlib.sha256(payload).hexdigest(),
                "lines": [line.__dict__ for line in lines],
            }
        )
    snapshot = destination / "line-snapshot.json"
    write_json(
        snapshot,
        {
            "schema_version": 1,
            "source_uri": PORTAL_URL,
            "retrieved_at": utc_now(),
            "reference_date": reference.isoformat(),
            "queries": records,
        },
    )
    return snapshot


def snapshot_lines(snapshot: Path) -> set[str]:
    value = cast(dict[str, Any], json.loads(snapshot.read_text(encoding="utf-8")))
    if value.get("schema_version") != 1:
        raise PipelineError(f"Unsupported line snapshot: {snapshot}")
    return {
        normalize_line(str(line["line"]))
        for query in cast(list[dict[str, Any]], value["queries"])
        for line in cast(list[dict[str, Any]], query["lines"])
    }


def _route_line(route_id: str) -> str | None:
    parts = route_id.split(":")
    if len(parts) >= 3 and parts[0] == "jdf" and parts[1] == "route" and parts[2].isdigit():
        return parts[2]
    return None


def _rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def _header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return next(csv.reader(stream))


class _Writer:
    def __init__(self, path: Path, header: list[str]) -> None:
        self._stream = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._stream, fieldnames=header, lineterminator="\n")
        self._writer.writeheader()
        self.rows = 0

    def write(self, row: dict[str, str]) -> None:
        self._writer.writerow(row)
        self.rows += 1

    def close(self) -> None:
        self._stream.close()


def _missing_coordinates(row: dict[str, str]) -> bool:
    try:
        latitude = float(row["stop_lat"])
        longitude = float(row["stop_lon"])
    except (KeyError, ValueError):
        return True
    return latitude == 0 and longitude == 0


def filter_gtfs(
    source: Path,
    output: Path,
    *,
    removed_lines: set[str],
    line_prefixes: Iterable[str],
) -> dict[str, Any]:
    """Filter an extracted GTFS directory into ``output``; return the report counts."""

    prefixes = tuple(line_prefixes)
    output.mkdir(parents=True, exist_ok=False)
    counts: Counter[str] = Counter(
        {
            "trips_removed_by_line": 0,
            "calls_removed_missing_coordinates": 0,
        }
    )

    stops = {row["stop_id"]: row for row in _rows(source / "stops.txt")}
    invalid_stops = {
        stop_id
        for stop_id, row in stops.items()
        if _missing_coordinates(row)
        or (
            row.get("parent_station") and _missing_coordinates(stops.get(row["parent_station"], {}))
        )
    }
    removed_routes: set[str] = set()
    removed_route_lines: set[str] = set()
    for row in _rows(source / "routes.txt"):
        line = _route_line(row["route_id"])
        if line is not None and (line in removed_lines or line.startswith(prefixes)):
            removed_routes.add(row["route_id"])
            removed_route_lines.add(line)
    trip_route: dict[str, str] = {}
    for row in _rows(source / "trips.txt"):
        if row["route_id"] not in removed_routes:
            trip_route[row["trip_id"]] = row["route_id"]
        else:
            counts["trips_removed_by_line"] += 1

    stop_times = source / "stop_times.txt"
    valid_calls: Counter[str] = Counter()
    for row in _rows(stop_times):
        if row["trip_id"] in trip_route and row["stop_id"] not in invalid_stops:
            valid_calls[row["trip_id"]] += 1
    kept_trips = {trip for trip in trip_route if valid_calls[trip] >= 2}
    counts["trips_removed_too_few_calls"] = len(trip_route) - len(kept_trips)

    used_stops: set[str] = set()
    writer = _Writer(output / "stop_times.txt", _header(stop_times))
    try:
        for row in _rows(stop_times):
            if row["trip_id"] not in kept_trips:
                continue
            if row["stop_id"] in invalid_stops:
                counts["calls_removed_missing_coordinates"] += 1
                continue
            used_stops.add(row["stop_id"])
            writer.write(row)
    finally:
        writer.close()

    kept_routes: set[str] = set()
    services: set[str] = set()
    shapes: set[str] = set()
    writer = _Writer(output / "trips.txt", _header(source / "trips.txt"))
    try:
        for row in _rows(source / "trips.txt"):
            if row["trip_id"] in kept_trips:
                kept_routes.add(row["route_id"])
                services.add(row["service_id"])
                if row.get("shape_id"):
                    shapes.add(row["shape_id"])
                writer.write(row)
    finally:
        writer.close()

    agencies: set[str] = set()
    writer = _Writer(output / "routes.txt", _header(source / "routes.txt"))
    try:
        for row in _rows(source / "routes.txt"):
            if row["route_id"] in kept_routes:
                agencies.add(row.get("agency_id", ""))
                writer.write(row)
    finally:
        writer.close()

    parents = {
        stops[stop]["parent_station"] for stop in used_stops if stops[stop].get("parent_station")
    }
    emitted_stops = used_stops | parents
    keep: dict[str, Callable[[dict[str, str]], bool]] = {
        "stops.txt": lambda row: row["stop_id"] in emitted_stops,
        "agency.txt": lambda row: row.get("agency_id", "") in agencies,
        "calendar.txt": lambda row: row["service_id"] in services,
        "calendar_dates.txt": lambda row: row["service_id"] in services,
        "shapes.txt": lambda row: row["shape_id"] in shapes,
        "transfers.txt": lambda row: (
            row.get("from_stop_id", "") in emitted_stops
            and row.get("to_stop_id", "") in emitted_stops
            and (not row.get("from_trip_id") or row["from_trip_id"] in kept_trips)
            and (not row.get("to_trip_id") or row["to_trip_id"] in kept_trips)
            and (not row.get("from_route_id") or row["from_route_id"] in kept_routes)
            and (not row.get("to_route_id") or row["to_route_id"] in kept_routes)
        ),
    }
    handled = {"stop_times.txt", "trips.txt", "routes.txt"}
    for path in sorted(source.iterdir()):
        if path.name in handled or not path.is_file():
            continue
        predicate = keep.get(path.name)
        if predicate is None:
            (output / path.name).write_bytes(path.read_bytes())
            counts[f"copied:{path.name}"] = 1
            continue
        writer = _Writer(output / path.name, _header(path))
        try:
            for row in _rows(path):
                if predicate(row):
                    writer.write(row)
        finally:
            writer.close()

    return {
        "routes_removed": len(removed_routes),
        "lines_removed": sorted(removed_route_lines),
        "routes_kept": len(kept_routes),
        "trips_kept": len(kept_trips),
        "stops_kept": len(emitted_stops),
        "stops_without_coordinates": len(invalid_stops),
        **dict(sorted(counts.items())),
    }


@contextmanager
def _package_gtfs(package: Path) -> Generator[Path]:
    try:
        with extracted_gtfs(package) as source:
            yield source
    except ProductionPackageError as error:
        raise PipelineError(str(error)) from error


def build_filtered_jdf(
    bundle: Path,
    destination: Path,
    *,
    reference: date,
    work: Path,
    rules_path: Path = RULES,
    line_snapshot: Path | None = None,
    post: PostFn = _post,
) -> Path:
    """Write ``destination/gtfs.zip`` and ``filter-report.json`` from a national JDF bundle."""

    rules = load_rules(rules_path)
    if line_snapshot is None:
        line_snapshot = fetch_line_snapshot(rules, reference, work / "line-portal", post=post)
    removed_lines = snapshot_lines(line_snapshot)
    destination.mkdir(parents=True, exist_ok=False)
    archive = bundle / "gtfs.zip"
    with (
        TemporaryDirectory(prefix="obehy-filtered-jdf-", dir=work) as temporary,
        _package_gtfs(bundle) as source,
    ):
        filtered = Path(temporary) / "filtered"
        report = filter_gtfs(
            source,
            filtered,
            removed_lines=removed_lines,
            line_prefixes=rules.line_prefixes,
        )
        verify_gtfs_stops(filtered)
        if report["trips_kept"] == 0:
            raise PipelineError("Filtered JDF GTFS contains no trips")
        identity = deterministic_zip(filtered, destination / "gtfs.zip")
    snapshot_copy = destination / "line-snapshot.json"
    snapshot_copy.write_bytes(line_snapshot.read_bytes())
    write_json(
        destination / "filter-report.json",
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "source_gtfs_sha256": file_digest(archive),
            "rules": {"path": rules_path.name, "sha256": file_digest(rules_path)},
            "line_snapshot_sha256": file_digest(snapshot_copy),
            "portal_lines": len(removed_lines),
            "gtfs": {"bytes": identity.bytes, "sha256": identity.sha256},
            **report,
        },
    )
    return destination
