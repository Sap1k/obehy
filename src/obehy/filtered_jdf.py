"""Filtered national JDF GTFS for consumers that merge it with regional feeds.

The filter mirrors https://github.com/0xaa55h/gtfs-processor: it drops CIS lines that other feeds
already publish and drops calls at stops without coordinates. The lines come from the merged
national JDF itself: preferred `LinExt.txt` rows of the listed integrated systems, lines of the
listed operators (`Dopravci.txt`), and fixed line-number prefixes. This reproduces the
portal.radekpapez.cz queries gtfs-processor uses, which read the same JDF data. Customs (JDF `$`)
stops are already non-boardable in JrUtil's output.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from obehy.national_jdf import verify_gtfs_stops
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import deterministic_zip, file_digest, utc_now, write_json
from obehy.production_package import ProductionPackageError, extracted_gtfs

RULES = Path(__file__).with_name("data") / "filtered-jdf" / "rules-v1.json"

# JDF 1.11 field counts, as JrUtil's merger writes them.
_LINKY_FIELDS = 17
_LINEXT_FIELDS = 7
_DOPRAVCI_FIELDS = 13
_ALTDOP_FIELDS = 15


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
    """Return a six-digit CIS line number."""

    text = value.strip()
    if not text.isdigit() or len(text) > 6:
        raise ValueError(f"Not a CIS line number: {value!r}")
    return text.zfill(6)


def _jdf_rows(archive: zipfile.ZipFile, name: str, fields: int) -> Iterator[list[str]]:
    with (
        archive.open(name) as raw,
        io.TextIOWrapper(raw, encoding="cp1250", newline="") as stream,
    ):
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            if line.endswith(";"):
                line = line[:-1]
            if not line:
                continue
            row = next(csv.reader([line]))
            if len(row) != fields:
                raise PipelineError(
                    f"Merged JDF {name} row has {len(row)} fields, expected {fields}: {line[:80]}"
                )
            yield row


def _jdf_date(value: str) -> date:
    if len(value) != 8 or not value.isdigit():
        raise PipelineError(f"Invalid JDF date: {value!r}")
    return date(int(value[4:8]), int(value[2:4]), int(value[0:2]))


def jdf_line_snapshot(
    merged_jdf: Path, rules: FilterRules, reference: date, destination: Path
) -> Path:
    """Write the lines matched by ``rules`` from the merged national JDF.

    Every line version still valid on or after ``reference`` counts, so a line whose current
    version is a short detour still matches through its regular version. An operator matches
    as the line's operator or as an alternative operator (`Altdop.txt`). An integrated system
    matches only through the preferred `LinExt` row, as on the line portal, so a line that
    merely also accepts another system's tariff is kept.
    """

    operator_names = set(rules.operators)
    ids_codes = {str(code) for code in rules.ids_codes}
    with zipfile.ZipFile(merged_jdf) as archive:
        names = {
            (row[0], row[12]): row[2]
            for row in _jdf_rows(archive, "Dopravci.txt", _DOPRAVCI_FIELDS)
        }
        # (line, line distinction) -> {(IČ, operator distinction)}
        operators: dict[tuple[str, str], set[tuple[str, str]]] = {}
        if "Altdop.txt" in archive.namelist():
            for row in _jdf_rows(archive, "Altdop.txt", _ALTDOP_FIELDS):
                operators.setdefault((row[0], row[14]), set()).add((row[2], row[13]))
        preferred: dict[tuple[str, str], set[str]] = {}
        for row in _jdf_rows(archive, "LinExt.txt", _LINEXT_FIELDS):
            if row[4] == "1":
                preferred.setdefault((row[0], row[6]), set()).add(row[2])
        by_operator: set[str] = set()
        by_system: dict[str, set[str]] = {code: set() for code in ids_codes}
        for row in _jdf_rows(archive, "Linky.txt", _LINKY_FIELDS):
            if _jdf_date(row[14]) < reference:
                continue
            line = normalize_line(row[0])
            version = (row[0], row[16])
            line_operators = {(row[2], row[15])} | operators.get(version, set())
            if any(names.get(operator) in operator_names for operator in line_operators):
                by_operator.add(line)
            for code in preferred.get(version, set()) & ids_codes:
                by_system[code].add(line)

    groups: list[tuple[str, str, set[str]]] = []
    if rules.operators:
        groups.append(("operators", ";".join(rules.operators), by_operator))
    groups += [("ids", str(code), by_system[str(code)]) for code in rules.ids_codes]
    for kind, value, lines in groups:
        if not lines:
            raise PipelineError(f"Merged JDF has no lines for {kind}={value} from {reference}")
    queries = [
        {"kind": kind, "value": value, "lines": sorted(lines)} for kind, value, lines in groups
    ]
    snapshot = destination / "line-snapshot.json"
    write_json(
        snapshot,
        {
            "schema_version": 2,
            "source": "jdf-linext",
            "merged_jdf_sha256": file_digest(merged_jdf),
            "created_at": utc_now(),
            "reference_date": reference.isoformat(),
            "queries": queries,
        },
    )
    return snapshot


def snapshot_lines(snapshot: Path) -> set[str]:
    value = cast(dict[str, Any], json.loads(snapshot.read_text(encoding="utf-8")))
    if value.get("schema_version") != 2:
        raise PipelineError(f"Unsupported line snapshot: {snapshot}")
    return {
        normalize_line(str(line))
        for query in cast(list[dict[str, Any]], value["queries"])
        for line in cast(list[str], query["lines"])
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
    merged_jdf: Path,
    rules_path: Path = RULES,
) -> Path:
    """Write ``destination/gtfs.zip`` and ``filter-report.json`` from a national JDF bundle.

    ``merged_jdf`` is the merged JDF the bundle was generated from; it decides which lines the
    regional feeds already publish.
    """

    rules = load_rules(rules_path)
    destination.mkdir(parents=True, exist_ok=False)
    line_snapshot = jdf_line_snapshot(merged_jdf, rules, reference, destination)
    removed_lines = snapshot_lines(line_snapshot)
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
    write_json(
        destination / "filter-report.json",
        {
            "schema_version": 2,
            "created_at": utc_now(),
            "source_gtfs_sha256": file_digest(archive),
            "rules": {"path": rules_path.name, "sha256": file_digest(rules_path)},
            "line_snapshot_sha256": file_digest(line_snapshot),
            "matched_lines": len(removed_lines),
            "gtfs": {"bytes": identity.bytes, "sha256": identity.sha256},
            **report,
        },
    )
    return destination
