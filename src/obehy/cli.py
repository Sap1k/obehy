"""One-command production feed pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from obehy import filtered_jdf, gvd, national_czptt, national_jdf, osm_snapshot, regional_overlay
from obehy.pipeline import jrutil
from obehy.pipeline.args import JobSetting, parse_jobs, parse_memory_budget, parse_year
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import file_digest, utc_now, write_json
from obehy.pipeline.process import CommandFn, run_command
from obehy.pipeline.reporting import BuildReporter, CommandProgress, Reporter
from obehy.production_package import (
    ProductionPackageError,
    package_digest,
    read_manifest,
)
from obehy.runtime_config import ConfigurationError, RuntimeConfig, load_runtime_config

POLICY = regional_overlay.POLICY
ProgressMode = Literal["auto", "rich", "plain", "off"]


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


@dataclass(frozen=True)
class _Run:
    """Directories of one production run; `partial_release` becomes `release` on success."""

    run_id: str
    release: Path
    partial_release: Path
    root: Path
    sources: Path
    logs: Path
    diagnostics: Path
    lock: Path

    @property
    def jdf_output(self) -> Path:
        return self.root / "national-jdf"

    @property
    def czptt_output(self) -> Path:
        return self.root / "national-czptt"


def _runtime_command(runtime: RuntimeConfig) -> list[str]:
    return jrutil.runtime_command(runtime.jrutil.directory, runtime.jrutil.command)


def _check_inputs(options: BuildOptions) -> Path:
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
    return geodata


def _acquire_lock(path: Path, run_id: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise PipelineError(f"Another production build holds {path}") from error
    os.write(descriptor, (json.dumps({"run_id": run_id, "started_at": utc_now()}) + "\n").encode())
    return descriptor


def _start_run(runtime: RuntimeConfig) -> tuple[_Run, int]:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    releases = runtime.artifact_root / "releases"
    root = runtime.workdir / "runs" / "production" / run_id
    run = _Run(
        run_id=run_id,
        release=releases / run_id,
        partial_release=releases / f".{run_id}.part",
        root=root,
        sources=root / "regional-sources",
        logs=root / "logs",
        diagnostics=root / "diagnostics",
        lock=runtime.artifact_root / ".production-build.lock",
    )
    lock_descriptor = _acquire_lock(run.lock, run_id)
    try:
        for directory in (run.partial_release, run.sources, run.logs, run.diagnostics):
            directory.mkdir(parents=True, exist_ok=False)
    except Exception:
        os.close(lock_descriptor)
        run.lock.unlink(missing_ok=True)
        raise
    return run, lock_descriptor


def _jdf_config(
    options: BuildOptions, geodata: Path, output: Path, reference_date: date
) -> national_jdf.BuildConfig:
    runtime = options.runtime
    return national_jdf.BuildConfig(
        output=output,
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
    )


def _czptt_config(options: BuildOptions, output: Path) -> national_czptt.BuildConfig:
    runtime = options.runtime
    return national_czptt.BuildConfig(
        output=output,
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
    )


def _validate_packages(
    packages: dict[str, Path],
    runtime: RuntimeConfig,
    run: _Run,
    reporter: Reporter,
    command_runner: CommandFn,
) -> dict[str, object]:
    manifests: dict[str, object] = {}
    for name, package in packages.items():
        command_runner(
            [*_runtime_command(runtime), "validate-package", str(package)],
            runtime.jrutil.directory or Path.cwd(),
            run.logs / f"validate-{name}.process.log",
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
    return manifests


def _release_record(
    options: BuildOptions,
    run: _Run,
    sources: Sequence[regional_overlay.Source],
    packages: dict[str, object],
    stages: list[dict[str, str]],
) -> dict[str, object]:
    outputs: dict[str, object] = {}
    if options.filtered_jdf:
        filtered = run.partial_release / "jdf-filtered"
        outputs["jdf_filtered"] = {
            "gtfs_sha256": file_digest(filtered / "gtfs.zip"),
            "filter_report_sha256": file_digest(filtered / "filter-report.json"),
        }
    return {
        "schema_version": 1,
        "run_id": run.run_id,
        "completed_at": utc_now(),
        "gvd_year": options.gvd_year,
        "options": {
            "jobs": options.jobs,
            "memory_budget": options.memory_budget,
            "estimated_posts": options.estimated_posts,
            "post_inference_policy": (
                str(options.post_inference_policy) if options.post_inference_policy else None
            ),
            "refresh_osm": options.refresh_osm,
            "czptt_operational_points": options.czptt_operational_points,
            "filtered_jdf": options.filtered_jdf,
        },
        "policy": {"path": str(POLICY), "sha256": file_digest(POLICY)},
        "sources": {
            source.source_id: json.loads(source.descriptor.read_text(encoding="utf-8"))
            for source in sources
        },
        "packages": packages,
        "outputs": outputs,
        "stages": stages,
    }


def _publish(options: BuildOptions, run: _Run) -> Release:
    os.replace(run.partial_release, run.release)
    release = run.release
    current = {
        "schema_version": 1,
        "run_id": run.run_id,
        "release": str(release.resolve()),
        "jdf": str((release / "jdf").resolve()),
        "czptt": str((release / "czptt").resolve()),
    }
    filtered = release / "jdf-filtered" if options.filtered_jdf else None
    if filtered is not None:
        current["jdf_filtered"] = str((filtered / "gtfs.zip").resolve())
    write_json(options.runtime.artifact_root / "current.json", current)
    if not options.keep_work:
        shutil.rmtree(run.jdf_output, ignore_errors=True)
        shutil.rmtree(run.czptt_output, ignore_errors=True)
    return Release(run.run_id, release, release / "jdf", release / "czptt", filtered)


def _record_failure(run: _Run, error: Exception, stages: list[dict[str, str]]) -> None:
    write_json(
        run.root / "failure.json",
        {
            "schema_version": 1,
            "failed_at": utc_now(),
            "error_type": type(error).__name__,
            "error": str(error),
            "completed_stages": stages,
            "partial_release": str(run.partial_release),
        },
    )


def build(
    options: BuildOptions,
    *,
    jdf_builder: Callable[..., Path] = national_jdf.build,
    czptt_builder: Callable[..., Path] = national_czptt.build,
    downloader: regional_overlay.DownloadGtfsFn = regional_overlay.download_gtfs,
    filtered_jdf_builder: Callable[..., Path] = filtered_jdf.build_filtered_jdf,
    command_runner: CommandFn = run_command,
) -> Release:
    runtime = options.runtime
    geodata = _check_inputs(options)
    run, lock_descriptor = _start_run(runtime)
    reporter = BuildReporter(options.progress)
    stages: list[dict[str, str]] = []

    def completed(name: str) -> None:
        stages.append({"name": name, "completed_at": utc_now()})

    try:
        if runtime.jrutil.directory is not None:
            command_runner(
                jrutil.build_command(runtime.jrutil.directory),
                runtime.jrutil.directory,
                run.logs / "jrutil-build.process.log",
                reporter,
                CommandProgress("Build JrUtil", stage="build-jrutil"),
            )
        completed("build-jrutil")

        reference_date = gvd.prague_today()
        jdf_builder(
            _jdf_config(options, geodata, run.jdf_output, reference_date), reporter=reporter
        )
        completed("national-jdf")
        jdf_bundle = run.jdf_output / "bundle"

        if options.filtered_jdf:
            filtered_work = run.root / "filtered-jdf"
            filtered_work.mkdir()
            filtered_jdf_builder(
                jdf_bundle,
                run.partial_release / "jdf-filtered",
                reference=reference_date,
                work=filtered_work,
                line_snapshot=options.line_filter_snapshot,
            )
            completed("filtered-jdf")

        sources = regional_overlay.snapshot_sources(run.sources, downloader)
        completed("regional-snapshots")

        regional_overlay.run(
            runtime_command=_runtime_command(runtime),
            cwd=runtime.jrutil.directory or Path.cwd(),
            base=jdf_bundle,
            output=run.partial_release / "jdf",
            sources=sources,
            gvd_year=options.gvd_year,
            jobs=options.jobs,
            memory_budget=options.memory_budget,
            diagnostics=run.diagnostics / "regional-overlay",
            log=run.logs / "regional-overlay.process.log",
            reporter=reporter,
            command_runner=command_runner,
        )
        completed("regional-overlay")

        czptt_builder(_czptt_config(options, run.czptt_output), reporter=reporter)
        shutil.move(run.czptt_output / "bundle", run.partial_release / "czptt")
        completed("national-czptt")

        packages = _validate_packages(
            {"jdf": run.partial_release / "jdf", "czptt": run.partial_release / "czptt"},
            runtime,
            run,
            reporter,
            command_runner,
        )
        completed("validation")

        write_json(
            run.partial_release / "release.json",
            _release_record(options, run, sources, packages, stages),
        )
        return _publish(options, run)
    except Exception as error:
        _record_failure(run, error, stages)
        raise
    finally:
        os.close(lock_descriptor)
        run.lock.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obehy")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build", help="build and publish the production feeds")
    command.add_argument("--config", type=Path)
    command.add_argument("--gvd-year", type=parse_year, default="auto")
    command.add_argument("--jobs", type=parse_jobs, default="auto")
    command.add_argument("--memory-budget", type=parse_memory_budget, default="auto")
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
