"""PID + IDS JMK regional overlay stage of the production build."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from obehy.pipeline.download import download_file
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import write_json
from obehy.pipeline.process import CommandFn
from obehy.pipeline.reporting import CommandProgress, Reporter

PID_URL = "https://data.pid.cz/PID_GTFS.zip"
IDS_JMK_URL = "https://kordis-jmk.cz/gtfs/gtfs.zip"
POLICY = (
    Path(__file__).with_name("data") / "regional-gtfs-overlay" / "pid-ids-jmk-production-v1.json"
)

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


def package_converter_version(package: Path) -> str:
    """The overlay runs the same JrUtil build that compiled its base package."""

    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    compiler = cast(dict[str, object], manifest).get("compiler")
    version = (
        cast(dict[str, object], compiler).get("version") if isinstance(compiler, dict) else None
    )
    if not isinstance(version, str) or not version:
        raise PipelineError(f"{package} manifest does not record a compiler version")
    return version


def run(
    *,
    runtime_command: Sequence[str],
    cwd: Path,
    base: Path,
    output: Path,
    sources: Sequence[Source],
    gvd_year: int,
    jobs: object,
    memory_budget: str,
    diagnostics: Path,
    log: Path,
    reporter: Reporter,
    command_runner: CommandFn,
    stop_registry: Path | None = None,
    stop_registry_candidates: Path | None = None,
) -> None:
    command = [
        *runtime_command,
        "regional-gtfs-overlay",
        f"--jobs={jobs}",
        f"--memory-budget={memory_budget}",
        f"--policy={POLICY}",
        f"--gvd-year={gvd_year}",
        f"--converter-version={package_converter_version(base)}",
    ]
    for source in sources:
        command += [
            f"--source={source.source_id}={source.payload}",
            f"--source-descriptor={source.source_id}={source.descriptor}",
        ]
    if stop_registry is not None:
        command.append(f"--stop-registry={stop_registry}")
        if stop_registry_candidates is not None:
            command.append(f"--stop-registry-candidates={stop_registry_candidates}")
    command += [f"--diagnostics-out={diagnostics}", str(base), str(output)]
    command_runner(
        command,
        cwd,
        log,
        reporter,
        CommandProgress("Overlay PID + IDS JMK", stage="regional-gtfs-overlay"),
    )
