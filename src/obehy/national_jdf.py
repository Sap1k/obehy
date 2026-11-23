"""The national road JDF sources and the checks of the road package's GTFS stops."""

from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path
from typing import Any

from obehy.pipeline import jrutil
from obehy.pipeline.download import DownloadFn, DownloadRecord
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import file_digest, write_json
from obehy.pipeline.reporting import Reporter, StageClock

VLD_URL = "https://portal.cisjr.cz/pub/JDF/JDF.zip"
DRAHY_URL = "https://portal.cisjr.cz/pub/draha/mestske/JDF.zip"
DEFAULT_POST_INFERENCE_POLICY = (
    Path(__file__).with_name("data") / "post-inference" / "learned-v1.json"
)


TRANSPORT_MODE_RULES = "transport-modes.csv"
ROUTE_PRESENTATION_RULES = "presentation.csv"
ROUTE_RULE_FILES = (TRANSPORT_MODE_RULES, ROUTE_PRESENTATION_RULES)


def geodata_manifest(geodata_directory: Path) -> dict[str, Any]:
    files = sorted(geodata_directory.rglob("*.csv"), key=lambda path: path.as_posix())
    if not files:
        raise PipelineError(f"Geodata directory contains no CSV files: {geodata_directory}")
    return {
        "repository": jrutil.git_identity(geodata_directory.parent),
        "directory": str(geodata_directory.resolve()),
        "files": [
            {
                "path": path.relative_to(geodata_directory).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": file_digest(path),
            }
            for path in files
        ],
    }


def require_route_rules(routes: Path) -> None:
    missing = [name for name in ROUTE_RULE_FILES if not (routes / name).is_file()]
    if missing:
        raise PipelineError(f"Route rules directory {routes} is missing {missing}")


def verify_gtfs_stops(gtfs: Path, reporter: Reporter | None = None) -> None:
    stops_path = gtfs / "stops.txt"
    stop_times_path = gtfs / "stop_times.txt"
    if not stops_path.is_file() or not stop_times_path.is_file():
        raise PipelineError("Bundle GTFS is missing stops.txt or stop_times.txt")

    referenced: set[str] = set()
    with stop_times_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            stop_id = row.get("stop_id", "").strip()
            if not stop_id:
                raise PipelineError("Bundle GTFS contains a stop_time without stop_id")
            referenced.add(stop_id)

    emitted: set[str] = set()
    boarding: set[str] = set()
    parents: set[str] = set()
    missing_coordinates: list[str] = []
    with stops_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        required = {"stop_id", "stop_lat", "stop_lon", "location_type", "parent_station"}
        if not rows.fieldnames or not required.issubset(rows.fieldnames):
            raise PipelineError("Bundle GTFS stops.txt is missing required stop fields")
        for row in rows:
            stop_id = row["stop_id"].strip()
            if not stop_id or stop_id in emitted:
                raise PipelineError(f"Bundle GTFS has blank or duplicate stop_id: {stop_id!r}")
            emitted.add(stop_id)
            if row["location_type"].strip() != "1":
                boarding.add(stop_id)
            parent = row["parent_station"].strip()
            if parent:
                parents.add(parent)
            try:
                latitude = float(row["stop_lat"])
                longitude = float(row["stop_lon"])
            except ValueError as error:
                raise PipelineError(f"Invalid coordinates for GTFS stop {stop_id}") from error
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise PipelineError(f"Out-of-range coordinates for GTFS stop {stop_id}")
            if latitude == 0 and longitude == 0:
                missing_coordinates.append(stop_id)

    dangling = referenced - emitted
    unreferenced = boarding - referenced
    missing_parents = parents - emitted
    extra_stations = (emitted - boarding) - parents
    if dangling or unreferenced or missing_parents or extra_stations:
        raise PipelineError(
            "Bundle GTFS stop reachability mismatch: "
            f"dangling={sorted(dangling)[:20]}, unreferenced={sorted(unreferenced)[:20]}, "
            f"missing_parents={sorted(missing_parents)[:20]}, "
            f"extra_stations={sorted(extra_stations)[:20]}"
        )
    if missing_coordinates and reporter is not None:
        reporter.problem(
            "warning",
            f"{len(missing_coordinates)} emitted GTFS stops use unresolved 0,0 coordinates; "
            f"stop_ids={','.join(missing_coordinates[:20])}",
        )


# (record name, URL, file name, stage) of the two national JDF exports.
SOURCES = (
    ("VLD", VLD_URL, "JDF_VLD.zip", "download-vld"),
    ("dráhy", DRAHY_URL, "JDF_drahy.zip", "download-drahy"),
)


def download_sources(
    download: DownloadFn,
    sources: Path,
    reporter: Reporter | None,
    clock: StageClock | None = None,
) -> tuple[DownloadRecord, DownloadRecord]:
    """Download both national exports into ``sources`` and record them in sources.json."""

    records: list[DownloadRecord] = []
    for name, url, filename, stage in SOURCES:
        if clock is not None:
            clock.start(stage)
        records.append(download(url, sources / filename, name, reporter))
    vld, drahy = records
    write_json(
        sources / "sources.json",
        {"schema_version": 1, "sources": [asdict(vld), asdict(drahy)]},
    )
    return vld, drahy
