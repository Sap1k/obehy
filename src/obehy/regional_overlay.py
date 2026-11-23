"""The PID and IDS JMK GTFS feeds the compiler overlays on the road timetable."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from obehy.pipeline.download import download_file
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import write_json
from obehy.pipeline.reporting import Reporter

PID_URL = "https://data.pid.cz/PID_GTFS.zip"
IDS_JMK_URL = "https://kordis-jmk.cz/gtfs/gtfs.zip"

DownloadGtfsFn = Callable[..., Path]


@dataclass(frozen=True)
class Source:
    source_id: str
    payload: Path
    descriptor: Path


def download_gtfs(
    url: str,
    source_id: str,
    destination: Path,
    *,
    require_api: bool,
    reporter: Reporter | None = None,
) -> Path:
    """Download one regional GTFS snapshot and write its checksum descriptor."""

    record = download_file(url, destination, source_id, reporter)
    with zipfile.ZipFile(destination) as archive:
        names = {name.casefold() for name in archive.namelist() if not name.endswith("/")}
    required = {"agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt"}
    missing = sorted(required - names)
    if missing:
        raise PipelineError(f"{source_id} GTFS is missing required files: {missing}")
    if require_api and "api.txt" not in names:
        raise PipelineError("IDS JMK GTFS is missing required api.txt")
    descriptor = destination.with_name(f"{source_id}-descriptor.json")
    write_json(
        descriptor,
        {
            "schema_version": 1,
            "source_id": source_id,
            "retrieved_at": record.retrieved_at,
            "source_uri": url,
            "payload_sha256": record.sha256,
        },
    )
    return descriptor


def snapshot_sources(directory: Path, downloader: DownloadGtfsFn = download_gtfs) -> list[Source]:
    directory.mkdir(parents=True, exist_ok=True)
    pid = directory / "pid-gtfs.zip"
    jmk = directory / "ids-jmk-gtfs.zip"
    return [
        Source("pid-gtfs", pid, downloader(PID_URL, "pid-gtfs", pid, require_api=False)),
        Source("ids-jmk-gtfs", jmk, downloader(IDS_JMK_URL, "ids-jmk-gtfs", jmk, require_api=True)),
    ]
