"""One-command production feed pipeline."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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

from obehy import (
    build_sources,
    filtered_jdf,
    gvd,
    national_czptt,
    national_jdf,
    osm_snapshot,
    regional_overlay,
)
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
from obehy.realtime import record, replay
from obehy.release import commands as release_commands
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
    routing_cache: bool = True
    czptt_source_cache: bool = True


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
    national_jdf.require_route_rules(_route_rules(runtime))
    for required in (_overlay_overrides(runtime), _filtered_jdf_rules(runtime)):
        if not required.exists():
            raise PipelineError(f"Geodata checkout is missing {required}")
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
        sources=root / "sources",
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


def _route_rules(runtime: RuntimeConfig) -> Path:
    return runtime.jrunify_ext_geodata_dir / "routes"


def _overlay_overrides(runtime: RuntimeConfig) -> Path:
    return runtime.jrunify_ext_geodata_dir / "overlay"


def _filtered_jdf_rules(runtime: RuntimeConfig) -> Path:
    return runtime.jrunify_ext_geodata_dir / "filtered-jdf" / "rules-v1.json"


def _stop_registry(runtime: RuntimeConfig) -> Path | None:
    """The geodata checkout's stop ID registry, once it has been created."""

    registry = runtime.jrunify_ext_geodata_dir / "registry"
    return registry if (registry / "stops.csv").is_file() else None


def _jdf_config(
    options: BuildOptions,
    geodata: Path,
    output: Path,
    reference_date: date,
    source_snapshot: Path | None = None,
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
        routing_cache_dir=runtime.routing_cache_dir if options.routing_cache else None,
        build_jrutil=False,
        gvd_year=options.gvd_year,
        reference_date=reference_date,
        stop_registry=_stop_registry(runtime),
        route_rules=_route_rules(runtime),
        source_snapshot=source_snapshot,
    )


def _czptt_config(
    options: BuildOptions, output: Path, source_snapshot: Path | None = None
) -> national_czptt.BuildConfig:
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
        source_snapshot=source_snapshot,
        jobs=options.jobs,
        memory_budget=options.memory_budget,
        keep_work=options.keep_work,
        progress=options.progress,
        build_jrutil=False,
        source_cache_dir=(runtime.czptt_source_cache_dir if options.czptt_source_cache else None),
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
    fetched: build_sources.FetchedSources,
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
                str(options.post_inference_policy or national_jdf.DEFAULT_POST_INFERENCE_POLICY)
                if options.estimated_posts
                else None
            ),
            "refresh_osm": options.refresh_osm,
            "czptt_operational_points": options.czptt_operational_points,
            "filtered_jdf": options.filtered_jdf,
            "routing_cache": options.routing_cache,
            "czptt_source_cache": options.czptt_source_cache,
        },
        "policy": {"path": str(POLICY), "sha256": file_digest(POLICY)},
        "retrieval": fetched.retrieval,
        "sources": {
            source.source_id: json.loads(source.descriptor.read_text(encoding="utf-8"))
            for source in fetched.regional
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
    downloader: regional_overlay.DownloadGtfsFn | None = None,
    filtered_jdf_builder: Callable[..., Path] = filtered_jdf.build_filtered_jdf,
    command_runner: CommandFn = run_command,
    jdf_source_fetcher: build_sources.JdfSourceFetcher = build_sources.fetch_jdf_sources,
    czptt_source_fetcher: build_sources.CzpttSourceFetcher = national_czptt.snapshot_sources,
) -> Release:
    runtime = options.runtime
    geodata = _check_inputs(options)
    run, lock_descriptor = _start_run(runtime)
    reporter = BuildReporter(options.progress)
    stages: list[dict[str, str]] = []

    def completed(name: str) -> None:
        stages.append({"name": name, "completed_at": utc_now()})

    try:
        # Every network source first: one retrieval date, and outages fail in minutes.
        fetched = build_sources.fetch_sources(
            run.sources,
            _czptt_config(options, run.czptt_output),
            reporter,
            jdf_fetcher=jdf_source_fetcher,
            czptt_fetcher=czptt_source_fetcher,
            regional_downloader=downloader,
        )
        completed("fetch-sources")
        reference_date = fetched.reference_date

        if runtime.jrutil.directory is not None:
            command_runner(
                jrutil.build_command(runtime.jrutil.directory),
                runtime.jrutil.directory,
                run.logs / "jrutil-build.process.log",
                reporter,
                CommandProgress("Build JrUtil", stage="build-jrutil"),
            )
        completed("build-jrutil")

        jdf_builder(
            _jdf_config(options, geodata, run.jdf_output, reference_date, fetched.jdf),
            reporter=reporter,
        )
        completed("national-jdf")
        jdf_bundle = run.jdf_output / "bundle"
        # Registry review files outlive the work directory in the release.
        registry_review = run.partial_release / "stop-registry"
        if (run.jdf_output / "stop-registry").is_dir():
            shutil.move(run.jdf_output / "stop-registry", registry_review)

        if options.filtered_jdf:
            filtered_work = run.root / "filtered-jdf"
            filtered_work.mkdir()
            filtered_jdf_builder(
                jdf_bundle,
                run.partial_release / "jdf-filtered",
                reference=reference_date,
                work=filtered_work,
                merged_jdf=run.jdf_output / "derived" / "merged-jdf.zip",
                rules_path=_filtered_jdf_rules(runtime),
            )
            completed("filtered-jdf")

        regional_overlay.run(
            runtime_command=_runtime_command(runtime),
            cwd=runtime.jrutil.directory or Path.cwd(),
            base=jdf_bundle,
            output=run.partial_release / "jdf",
            sources=fetched.regional,
            gvd_year=options.gvd_year,
            jobs=options.jobs,
            memory_budget=options.memory_budget,
            diagnostics=run.diagnostics / "regional-overlay",
            log=run.logs / "regional-overlay.process.log",
            reporter=reporter,
            command_runner=command_runner,
            stop_registry=_stop_registry(runtime),
            stop_registry_candidates=registry_review / "place-candidates.csv",
            overrides_root=_overlay_overrides(runtime),
            route_rules=_route_rules(runtime),
        )
        completed("regional-overlay")

        czptt_builder(_czptt_config(options, run.czptt_output, fetched.czptt), reporter=reporter)
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
            _release_record(options, run, fetched, packages, stages),
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
        "--no-routing-cache",
        action="store_true",
        help="route every post-inference context instead of reusing the routing cache",
    )
    command.add_argument(
        "--no-czptt-source-cache",
        action="store_true",
        help="download every CZPTT source object instead of reusing the source cache",
    )
    realtime = commands.add_parser("rt", help="realtime tools")
    realtime_commands = realtime.add_subparsers(dest="rt_command", required=True)
    recorder = realtime_commands.add_parser(
        "record", help="archive realtime source payloads without processing them"
    )
    recorder.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    recorder.add_argument(
        "--sources",
        type=lambda text: [source.strip() for source in text.split(",") if source.strip()],
        help="comma-separated source IDs (default: every channel in the manifest)",
    )
    recorder.add_argument("--manifest", type=Path, default=record.MANIFEST)
    recorder.add_argument("--once", action="store_true", help="poll every channel once and exit")
    recorder.add_argument(
        "--duration", type=record.parse_duration, help="stop after e.g. 30m, 6h or 2d"
    )
    replayer = realtime_commands.add_parser(
        "replay", help="resolve archived realtime payloads against a release directory"
    )
    replayer.add_argument("--release", type=Path, required=True, help="release directory")
    replayer.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    replayer.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    replayer.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    replayer.add_argument(
        "--sources",
        type=lambda text: [source.strip() for source in text.split(",") if source.strip()],
        help=f"comma-separated source IDs (default: {','.join(replay.SOURCES)})",
    )
    replayer.add_argument("--manifest", type=Path, default=record.MANIFEST)
    replayer.add_argument("--out", type=Path, required=True, help="output directory")
    release_commands.add_parsers(commands)
    return parser


def _record(args: argparse.Namespace) -> int:
    try:
        channels = record.select_channels(
            record.load_channels(cast(Path, args.manifest)),
            cast(list[str] | None, args.sources),
        )
    except (OSError, record.ManifestError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            record.record(
                channels,
                cast(Path, args.archive),
                once=cast(bool, args.once),
                duration_s=cast(float | None, args.duration),
            )
        )
    return 0


def _replay(args: argparse.Namespace) -> int:
    try:
        channels = record.select_channels(
            record.load_channels(cast(Path, args.manifest)),
            cast(list[str] | None, args.sources) or list(replay.SOURCES),
        )
        document = replay.replay(
            replay.ReplayOptions(
                release=cast(Path, args.release),
                archive=cast(Path, args.archive),
                start=cast(date, args.start),
                end=cast(date, args.end),
                channels=[(channel.source, channel.channel) for channel in channels],
                out=cast(Path, args.out),
            )
        )
    except (OSError, record.ManifestError, replay.ReplayError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for line in replay.summary_lines(document):
        print(line)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "rt":
        return _replay(args) if args.rt_command == "replay" else _record(args)
    if args.command in ("db", "release"):
        return release_commands.run(args)
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
                routing_cache=not cast(bool, args.no_routing_cache),
                czptt_source_cache=not cast(bool, args.no_czptt_source_cache),
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
