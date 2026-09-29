"""One-command production feed pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.request import Request, urlopen

from obehy import filtered_jdf, gvd, national_czptt, national_jdf, osm_snapshot
from obehy.national_jdf import BuildReporter, CommandProgress, PipelineError
from obehy.pipeline_support import file_digest, utc_now, write_json
from obehy.production_package import (
    ProductionPackageError,
    package_digest,
    read_manifest,
)
from obehy.runtime_config import ConfigurationError, RuntimeConfig, load_runtime_config

PID_URL = "https://data.pid.cz/PID_GTFS.zip"
IDS_JMK_URL = "https://kordis-jmk.cz/gtfs/gtfs.zip"
POLICY = (
    Path(__file__).with_name("data") / "regional-gtfs-overlay" / "pid-ids-jmk-production-v1.json"
)
ProgressMode = Literal["auto", "rich", "plain", "off"]
JobSetting = Literal["auto"] | int


@dataclass(frozen=True)
class BuildOptions:
    runtime: RuntimeConfig
    gvd_year: int
    jobs: JobSetting = "auto"
    memory_budget: str = "auto"
    progress: ProgressMode = "auto"
    keep_work: bool = False
    estimated_posts: bool = False
    post_inference_policy: Path | None = None
    refresh_osm: bool = False
    czptt_operational_points: national_czptt.OperationalPointMode = "sidecar"
    filtered_jdf: bool = True
    line_filter_snapshot: Path | None = None


@dataclass(frozen=True)
class Release:
    run_id: str
    root: Path
    jdf: Path
    czptt: Path
    jdf_filtered: Path | None = None


def _runtime_command(runtime: RuntimeConfig) -> list[str]:
    if runtime.jrutil.command is not None:
        return list(runtime.jrutil.command)
    assert runtime.jrutil.directory is not None
    return [
        "dotnet",
        str(
            runtime.jrutil.directory
            / "jrutil-multitool"
            / "bin"
            / "Release"
            / "net10.0"
            / "jrutil-multitool.dll"
        ),
    ]


def _download_gtfs(url: str, source_id: str, destination: Path, *, require_api: bool) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": "Obehy/0.1 production-feed-builder"})
    retrieved_at = utc_now()
    try:
        with urlopen(request, timeout=120) as response, partial.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        os.replace(partial, destination)
        with zipfile.ZipFile(destination) as archive:
            names = {name.casefold() for name in archive.namelist() if not name.endswith("/")}
            required = {"agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt"}
            missing = sorted(required - names)
            if missing:
                raise PipelineError(f"{source_id} GTFS is missing required files: {missing}")
            if require_api and "api.txt" not in names:
                raise PipelineError("IDS JMK GTFS is missing required api.txt")
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    descriptor = destination.with_name(f"{source_id}-descriptor.json")
    write_json(
        descriptor,
        {
            "schema_version": 1,
            "source_id": source_id,
            "retrieved_at": retrieved_at,
            "source_uri": url,
            "payload_sha256": file_digest(destination),
        },
    )
    return descriptor


def _enrich(package: Path) -> Path:
    """Future immutable shape-enrichment boundary; currently a pass-through."""

    return package


def _acquire_lock(path: Path, run_id: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise PipelineError(f"Another production build holds {path}") from error
    os.write(descriptor, (json.dumps({"run_id": run_id, "started_at": utc_now()}) + "\n").encode())
    return descriptor


def build(
    options: BuildOptions,
    *,
    jdf_builder: Callable[..., Path] = national_jdf.build,
    czptt_builder: Callable[..., Path] = national_czptt.build,
    downloader: Callable[..., Path] = _download_gtfs,
    filtered_jdf_builder: Callable[..., Path] = filtered_jdf.build_filtered_jdf,
    command_runner: national_jdf.CommandFn = national_jdf.run_command,
) -> Release:
    runtime = options.runtime
    geodata = runtime.jrunify_ext_geodata_dir / "other"
    if not geodata.is_dir():
        raise PipelineError(f"Geodata directory does not exist: {geodata}")
    national_jdf.geodata_manifest(geodata)
    if not POLICY.is_file():
        raise PipelineError(f"Production overlay policy is missing: {POLICY}")
    if options.refresh_osm:
        osm_snapshot.build_snapshot(runtime)
    osm_snapshot.validate_snapshot(runtime.osm_file, runtime.workdir, full_hash=True)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    releases = runtime.artifact_root / "releases"
    release = releases / run_id
    partial_release = releases / f".{run_id}.part"
    run_root = runtime.workdir / "runs" / "production" / run_id
    sources = run_root / "regional-sources"
    logs = run_root / "logs"
    diagnostics = run_root / "diagnostics"
    lock = runtime.artifact_root / ".production-build.lock"
    lock_descriptor = _acquire_lock(lock, run_id)
    try:
        for directory in (partial_release, sources, logs, diagnostics):
            directory.mkdir(parents=True, exist_ok=False)
    except Exception:
        os.close(lock_descriptor)
        lock.unlink(missing_ok=True)
        raise
    reporter = BuildReporter(options.progress)
    stages: list[dict[str, str]] = []

    def completed(name: str) -> None:
        stages.append({"name": name, "completed_at": utc_now()})

    try:
        if runtime.jrutil.directory is not None:
            project = runtime.jrutil.directory / "jrutil-multitool" / "jrutil-multitool.fsproj"
            if not project.is_file():
                raise PipelineError(f"JrUtil multitool project does not exist: {project}")
            command_runner(
                ["dotnet", "build", str(project), "-c", "Release", "--no-restore"],
                runtime.jrutil.directory,
                logs / "jrutil-build.process.log",
                reporter,
                CommandProgress("Build JrUtil", stage="build-jrutil"),
            )
        completed("build-jrutil")

        jdf_output = run_root / "national-jdf"
        reference_date = gvd.prague_today()
        jdf_builder(
            national_jdf.BuildConfig(
                output=jdf_output,
                workdir=runtime.workdir,
                osm_file=runtime.osm_file,
                jrutil_root=runtime.jrutil.directory,
                jrutil_command=runtime.jrutil.command,
                geodata_root=geodata,
                keep_work=options.keep_work,
                progress=options.progress,
                jobs=options.jobs,
                memory_budget=options.memory_budget,
                estimated_posts=options.estimated_posts,
                post_inference_policy=options.post_inference_policy,
                build_jrutil=False,
                gvd_year=options.gvd_year,
                reference_date=reference_date,
            ),
            reporter=reporter,
        )
        completed("national-jdf")

        if options.filtered_jdf:
            filtered_work = run_root / "filtered-jdf"
            filtered_work.mkdir()
            filtered_jdf_builder(
                jdf_output / "bundle",
                partial_release / "jdf-filtered",
                reference=reference_date,
                work=filtered_work,
                line_snapshot=options.line_filter_snapshot,
            )
            completed("filtered-jdf")

        pid = sources / "pid-gtfs.zip"
        jmk = sources / "ids-jmk-gtfs.zip"
        pid_descriptor = downloader(PID_URL, "pid-gtfs", pid, require_api=False)
        jmk_descriptor = downloader(IDS_JMK_URL, "ids-jmk-gtfs", jmk, require_api=True)
        completed("regional-snapshots")

        jdf_package = partial_release / "jdf"
        overlay_command = [
            *_runtime_command(runtime),
            "regional-gtfs-overlay",
            f"--jobs={options.jobs}",
            f"--memory-budget={options.memory_budget}",
            f"--policy={POLICY}",
            f"--gvd-year={options.gvd_year}",
            f"--source=pid-gtfs={pid}",
            f"--source-descriptor=pid-gtfs={pid_descriptor}",
            f"--source=ids-jmk-gtfs={jmk}",
            f"--source-descriptor=ids-jmk-gtfs={jmk_descriptor}",
            f"--diagnostics-out={diagnostics / 'regional-overlay'}",
            str(jdf_output / "bundle"),
            str(jdf_package),
        ]
        command_runner(
            overlay_command,
            runtime.jrutil.directory or Path.cwd(),
            logs / "regional-overlay.process.log",
            reporter,
            CommandProgress("Overlay PID + IDS JMK", stage="regional-gtfs-overlay"),
        )
        completed("regional-overlay")

        czptt_output = run_root / "national-czptt"
        czptt_builder(
            national_czptt.BuildConfig(
                output=czptt_output,
                workdir=runtime.workdir,
                osm_file=runtime.osm_file,
                # CZPTT reads rail/SR70.csv from the snapshot root, not from other/.
                geodata_root=runtime.jrunify_ext_geodata_dir,
                jrutil_root=runtime.jrutil.directory,
                jrutil_command=runtime.jrutil.command,
                timetable_year=options.gvd_year,
                operational_points=options.czptt_operational_points,
                jobs=options.jobs,
                memory_budget=options.memory_budget,
                keep_work=options.keep_work,
                progress=options.progress,
                build_jrutil=False,
            ),
            reporter=reporter,
        )
        shutil.move(czptt_output / "bundle", partial_release / "czptt")
        completed("national-czptt")

        final_jdf = _enrich(jdf_package)
        final_czptt = _enrich(partial_release / "czptt")
        manifests = {}
        for name, package in (("jdf", final_jdf), ("czptt", final_czptt)):
            command_runner(
                [*_runtime_command(runtime), "validate-package", str(package)],
                runtime.jrutil.directory or Path.cwd(),
                logs / f"validate-{name}.process.log",
                reporter,
                CommandProgress(f"Validate {name.upper()} package", stage="validate-package"),
            )
            manifest = read_manifest(package, require_publication=True)
            manifests[name] = {
                "manifest_sha256": file_digest(package / "manifest.json"),
                "package_sha256": package_digest(package),
                "feed_version": manifest.get("feed_version"),
                "compiler": manifest.get("compiler"),
            }
        completed("enrichment-and-validation")

        outputs: dict[str, object] = {}
        if options.filtered_jdf:
            report = partial_release / "jdf-filtered" / "filter-report.json"
            outputs["jdf_filtered"] = {
                "gtfs_sha256": file_digest(partial_release / "jdf-filtered" / "gtfs.zip"),
                "filter_report_sha256": file_digest(report),
            }

        write_json(
            partial_release / "release.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "completed_at": utc_now(),
                "gvd_year": options.gvd_year,
                "options": {
                    "jobs": options.jobs,
                    "memory_budget": options.memory_budget,
                    "estimated_posts": options.estimated_posts,
                    "post_inference_policy": (
                        str(options.post_inference_policy)
                        if options.post_inference_policy
                        else None
                    ),
                    "refresh_osm": options.refresh_osm,
                    "czptt_operational_points": options.czptt_operational_points,
                    "filtered_jdf": options.filtered_jdf,
                },
                "policy": {"path": str(POLICY), "sha256": file_digest(POLICY)},
                "sources": {
                    "pid-gtfs": json.loads(pid_descriptor.read_text(encoding="utf-8")),
                    "ids-jmk-gtfs": json.loads(jmk_descriptor.read_text(encoding="utf-8")),
                },
                "packages": manifests,
                "outputs": outputs,
                "stages": stages,
            },
        )
        os.replace(partial_release, release)
        current = {
            "schema_version": 1,
            "run_id": run_id,
            "release": str(release.resolve()),
            "jdf": str((release / "jdf").resolve()),
            "czptt": str((release / "czptt").resolve()),
        }
        filtered = release / "jdf-filtered" if options.filtered_jdf else None
        if filtered is not None:
            current["jdf_filtered"] = str((filtered / "gtfs.zip").resolve())
        write_json(runtime.artifact_root / "current.json", current)
        if not options.keep_work:
            shutil.rmtree(jdf_output, ignore_errors=True)
            shutil.rmtree(czptt_output, ignore_errors=True)
        return Release(run_id, release, release / "jdf", release / "czptt", filtered)
    except Exception as error:
        write_json(
            run_root / "failure.json",
            {
                "schema_version": 1,
                "failed_at": utc_now(),
                "error_type": type(error).__name__,
                "error": str(error),
                "completed_stages": stages,
                "partial_release": str(partial_release),
            },
        )
        raise
    finally:
        os.close(lock_descriptor)
        lock.unlink(missing_ok=True)


def _year(value: str) -> int | Literal["auto"]:
    if value == "auto":
        return "auto"
    try:
        year = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be auto or a timetable year") from error
    if year < 2000:
        raise argparse.ArgumentTypeError("must be auto or a timetable year")
    return year


def _jobs(value: str) -> JobSetting:
    if value == "auto":
        return "auto"
    try:
        jobs = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be auto or a positive integer") from error
    if jobs <= 0:
        raise argparse.ArgumentTypeError("must be auto or a positive integer")
    return jobs


def _memory_budget(value: str) -> str:
    if not re.fullmatch(r"(?i)(?:auto|[0-9]+(?:\.[0-9]+)?(?:KiB|MiB|GiB))", value):
        raise argparse.ArgumentTypeError("must be auto or a size such as 10GiB")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obehy")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build", help="build and publish the production feeds")
    command.add_argument("--config", type=Path)
    command.add_argument("--gvd-year", type=_year, default="auto")
    command.add_argument("--jobs", type=_jobs, default="auto")
    command.add_argument("--memory-budget", type=_memory_budget, default="auto")
    command.add_argument("--progress", choices=("auto", "rich", "plain", "off"), default="auto")
    command.add_argument("--keep-work", action="store_true")
    command.add_argument("--estimated-posts", action="store_true")
    command.add_argument("--post-inference-policy", type=Path)
    command.add_argument("--refresh-osm", action="store_true")
    command.add_argument(
        "--czptt-operational-points", choices=("sidecar", "gtfs"), default="sidecar"
    )
    command.add_argument("--skip-filtered-jdf", action="store_true")
    command.add_argument(
        "--line-filter-snapshot",
        type=Path,
        help="replay a saved line-snapshot.json instead of querying the line portal",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime = load_runtime_config(cast(Path | None, args.config))
        requested_year = cast(int | Literal["auto"], args.gvd_year)
        gvd_year = gvd.resolve_timetable_year(requested_year)
        result = build(
            BuildOptions(
                runtime=runtime,
                gvd_year=gvd_year,
                jobs=cast(JobSetting, args.jobs),
                memory_budget=cast(str, args.memory_budget),
                progress=cast(ProgressMode, args.progress),
                keep_work=cast(bool, args.keep_work),
                estimated_posts=cast(bool, args.estimated_posts),
                post_inference_policy=cast(Path | None, args.post_inference_policy),
                refresh_osm=cast(bool, args.refresh_osm),
                czptt_operational_points=cast(
                    national_czptt.OperationalPointMode, args.czptt_operational_points
                ),
                filtered_jdf=not cast(bool, args.skip_filtered_jdf),
                line_filter_snapshot=cast(Path | None, args.line_filter_snapshot),
            )
        )
    except (
        ConfigurationError,
        OSError,
        PipelineError,
        ProductionPackageError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"JDF package: {result.jdf}")
    print(f"CZPTT package: {result.czptt}")
    if result.jdf_filtered is not None:
        print(f"Filtered JDF GTFS: {result.jdf_filtered / 'gtfs.zip'}")
    release_metadata = json.loads((result.root / "release.json").read_text(encoding="utf-8"))
    for source_id, source in sorted(release_metadata["sources"].items()):
        print(f"{source_id}: retrieved {source['retrieved_at']}, sha256 {source['payload_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
