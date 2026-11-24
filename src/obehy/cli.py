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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from obehy import (
    build_sources,
    gvd,
    national_czptt,
    national_jdf,
    osm_snapshot,
    regional_overlay,
    route_shapes,
    static_build,
)
from obehy.api import commands as api_commands
from obehy.pipeline import jrutil
from obehy.pipeline.args import JobSetting, parse_jobs, parse_run_id, parse_year
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import file_digest, utc_now, write_json
from obehy.pipeline.process import CommandFn, run_command
from obehy.pipeline.reporting import BuildReporter, CommandProgress, Reporter
from obehy.production_package import (
    ProductionPackageError,
    extracted_gtfs,
    package_digest,
    read_manifest,
)
from obehy.realtime import commands as realtime_commands
from obehy.reference import commands as reference_commands
from obehy.release import commands as release_commands
from obehy.runtime_config import ConfigurationError, RuntimeConfig, load_runtime_config

ProgressMode = Literal["auto", "rich", "plain", "off"]
GAZETTEER_FILES = (
    "cz_regions.json.gz",
    "eur_countries.json.gz",
    "cz_towns.json.gz",
    "eur_towns_countries.csv",
)


@dataclass(frozen=True)
class BuildOptions:
    runtime: RuntimeConfig
    gvd_year: int
    jobs: JobSetting = "auto"
    progress: ProgressMode = "auto"
    keep_work: bool = False
    estimated_posts: bool = False
    post_inference_policy: Path | None = None
    refresh_osm: bool = False
    filtered_jdf: bool = True
    routing_cache: bool = True
    czptt_source_cache: bool = True
    route_shapes: bool = True
    run_id: str | None = None


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


@dataclass(frozen=True)
class _Osm:
    """The validated OSM snapshot and the extracts the compiler reads."""

    merge_key: str
    railway_locations: Path
    post_candidates: Path
    transit_routing_base: Path


def _post_inference_policy(options: BuildOptions) -> Path:
    return options.post_inference_policy or national_jdf.DEFAULT_POST_INFERENCE_POLICY


def _check_inputs(options: BuildOptions) -> _Osm:
    runtime = options.runtime
    geodata = runtime.jrunify_ext_geodata_dir / "other"
    if not geodata.is_dir():
        raise PipelineError(f"Geodata directory does not exist: {geodata}")
    national_jdf.geodata_manifest(geodata)
    national_jdf.require_route_rules(_route_rules(runtime))
    required = [_overlay_overrides(runtime)]
    if options.filtered_jdf:
        required.append(_filtered_jdf_rules(runtime))
    if options.estimated_posts:
        required.append(_post_inference_policy(options))
    required += [_gazetteer(runtime) / name for name in GAZETTEER_FILES]
    for path in required:
        if not path.is_file():
            raise PipelineError(f"Build input is missing: {path}")
    if options.refresh_osm:
        osm_snapshot.build_snapshot(runtime)
    snapshot = osm_snapshot.validate_snapshot(runtime.osm_file, runtime.workdir, full_hash=True)
    merge_key = cast(str, snapshot["merge_key"])
    workdir = runtime.workdir
    return _Osm(
        merge_key=merge_key,
        railway_locations=osm_snapshot.validate_railway_locations(workdir, merge_key),
        post_candidates=osm_snapshot.validate_jdf_post_candidates(workdir, merge_key),
        transit_routing_base=osm_snapshot.validate_jdf_transit_routing_base(workdir, merge_key),
    )


def _acquire_lock(path: Path, run_id: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise PipelineError(f"Another production build holds {path}") from error
    os.write(descriptor, (json.dumps({"run_id": run_id, "started_at": utc_now()}) + "\n").encode())
    return descriptor


def _start_run(runtime: RuntimeConfig, run_id: str | None) -> tuple[_Run, int]:
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
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


def _gazetteer(runtime: RuntimeConfig) -> Path:
    return runtime.jrunify_ext_geodata_dir / "gazetteer"


def _route_rules(runtime: RuntimeConfig) -> Path:
    return runtime.jrunify_ext_geodata_dir / "routes"


def _overlay_overrides(runtime: RuntimeConfig) -> Path:
    return runtime.jrunify_ext_geodata_dir / "overlay" / "pid-stop-overrides.csv"


def _filtered_jdf_rules(runtime: RuntimeConfig) -> Path:
    return runtime.jrunify_ext_geodata_dir / "filtered-jdf" / "rules-v1.json"


def _stop_registry(runtime: RuntimeConfig) -> Path | None:
    """The geodata checkout's stop ID registry, once it has been created."""

    registry = runtime.jrunify_ext_geodata_dir / "registry"
    return registry if (registry / "stops.csv").is_file() else None


def _czptt_config(options: BuildOptions) -> national_czptt.BuildConfig:
    runtime = options.runtime
    return national_czptt.BuildConfig(
        # The snapshot takes rail/SR70.csv from the geodata root, not from other/.
        geodata_root=runtime.jrunify_ext_geodata_dir,
        timetable_year=options.gvd_year,
        source_cache_dir=(runtime.czptt_source_cache_dir if options.czptt_source_cache else None),
    )


def _build_spec(
    options: BuildOptions,
    run: _Run,
    osm: _Osm,
    fetched: build_sources.FetchedSources,
    shapes: route_shapes.RouteShapes | None,
) -> static_build.BuildSpec:
    runtime = options.runtime
    registry = _stop_registry(runtime)
    regional = {source.source_id: source.payload for source in fetched.regional}
    return static_build.BuildSpec(
        timetable_year=options.gvd_year,
        czptt_snapshot=fetched.czptt,
        railway_osm=osm.railway_locations,
        czptt_output=run.partial_release / "czptt",
        catalogues=runtime.jrunify_ext_geodata_dir / "other",
        gazetteer=_gazetteer(runtime),
        post_candidates_osm=osm.post_candidates,
        route_rules=_route_rules(runtime),
        reference_date=fetched.reference_date,
        jdf_output=run.partial_release / "jdf",
        vld_archive=fetched.jdf / "JDF_VLD.zip",
        drahy_archive=fetched.jdf / "JDF_drahy.zip",
        overlay_reports=run.diagnostics / "regional-overlay",
        pid_feed=regional["pid-gtfs"],
        pid_overrides=_overlay_overrides(runtime),
        ids_jmk_feed=regional["ids-jmk-gtfs"],
        registry=registry,
        registry_candidates=(
            run.partial_release / "stop-registry" / "stop-candidates.csv"
            if registry is not None
            else None
        ),
        posts=(
            static_build.Posts(
                routing=osm.transit_routing_base,
                policy=_post_inference_policy(options),
                reports=run.diagnostics / "posts",
                cache=runtime.routing_cache_dir if options.routing_cache else None,
            )
            if options.estimated_posts
            else None
        ),
        filtered=(
            static_build.Filtered(
                rules=_filtered_jdf_rules(runtime), output=run.partial_release / "jdf-filtered"
            )
            if options.filtered_jdf
            else None
        ),
        shapes=shapes,
    )


def _keep_registry_candidates(runtime: RuntimeConfig, run: _Run) -> None:
    """Put the post and place candidates next to the stop candidates the registry review reads."""

    if _stop_registry(runtime) is None:
        return
    review = run.partial_release / "stop-registry"
    for report, name in (
        (run.diagnostics / "posts" / "post_candidates.csv", "post-candidates.csv"),
        (
            run.diagnostics / "regional-overlay" / "overlay_place_candidates.csv",
            "place-candidates.csv",
        ),
    ):
        if report.is_file():
            review.mkdir(exist_ok=True)
            shutil.copy2(report, review / name)


def _check_stops(packages: dict[str, Path], czptt_snapshot: Path, reporter: Reporter) -> None:
    with extracted_gtfs(packages["jdf"]) as gtfs:
        national_jdf.verify_gtfs_stops(gtfs, reporter)
    with extracted_gtfs(packages["czptt"]) as gtfs:
        national_czptt.verify_gtfs_stops(gtfs, czptt_snapshot / "sr70" / "SR70.csv")


def _package_summaries(packages: dict[str, Path]) -> dict[str, object]:
    manifests: dict[str, object] = {}
    for name, package in packages.items():
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
    runtime = options.runtime
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
            "estimated_posts": options.estimated_posts,
            "post_inference_policy": (
                str(_post_inference_policy(options)) if options.estimated_posts else None
            ),
            "refresh_osm": options.refresh_osm,
            "filtered_jdf": options.filtered_jdf,
            "routing_cache": options.routing_cache,
            "czptt_source_cache": options.czptt_source_cache,
            "route_shapes": options.route_shapes,
        },
        "compiler": jrutil.provenance(runtime.jrutil.directory, runtime.jrutil.command),
        "retrieval": fetched.retrieval,
        "sources": {
            source.source_id: json.loads(source.descriptor.read_text(encoding="utf-8"))
            for source in fetched.regional
        },
        "packages": packages,
        "outputs": outputs,
        "route_shapes": route_shapes.summaries(
            {"jdf": run.partial_release / "jdf", "czptt": run.partial_release / "czptt"}
        ),
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
        shutil.rmtree(run.sources, ignore_errors=True)
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
    downloader: regional_overlay.DownloadGtfsFn | None = None,
    command_runner: CommandFn = run_command,
    jdf_source_fetcher: build_sources.JdfSourceFetcher = build_sources.fetch_jdf_sources,
    czptt_source_fetcher: build_sources.CzpttSourceFetcher = national_czptt.snapshot_sources,
) -> Release:
    runtime = options.runtime
    osm = _check_inputs(options)
    run, lock_descriptor = _start_run(runtime, options.run_id)
    reporter = BuildReporter(options.progress)
    stages: list[dict[str, str]] = []

    def completed(name: str) -> None:
        stages.append({"name": name, "completed_at": utc_now()})

    try:
        # Every network source first: one retrieval date, and outages fail in minutes.
        fetched = build_sources.fetch_sources(
            run.sources,
            _czptt_config(options),
            reporter,
            jdf_fetcher=jdf_source_fetcher,
            czptt_fetcher=czptt_source_fetcher,
            regional_downloader=downloader,
        )
        completed("fetch-sources")

        if runtime.jrutil.directory is not None:
            command_runner(
                jrutil.build_command(runtime.jrutil.directory),
                runtime.jrutil.directory,
                run.logs / "jrutil-build.process.log",
                reporter,
                CommandProgress("Build jrutil"),
            )
        completed("build-jrutil")

        # Optional: the compiler's route-shapes phase in both packages.
        shapes: route_shapes.RouteShapes | None = None
        if options.route_shapes:
            shapes, skipped = route_shapes.prepare(
                companion=runtime.route_shapes_companion,
                osm_file=runtime.osm_file,
                osm_key=lambda: osm.merge_key,
                run_dir=run.root,
                cache=runtime.route_shapes_cache_dir or runtime.workdir / "cache" / "route-shapes",
                log=run.logs / "route-shapes-graph.process.log",
                reporter=reporter,
                command_runner=command_runner,
            )
            if shapes is None:
                reporter.stage(f"Route shapes skipped: {skipped}")
                stages.append({"name": "route-shapes-graph", "skipped": cast(str, skipped)})
            else:
                completed("route-shapes-graph")

        static_build.run(
            _build_spec(options, run, osm, fetched, shapes),
            root=run.root,
            compiler=jrutil.runtime_command(runtime.jrutil.directory, runtime.jrutil.command),
            jobs=options.jobs,
            log=run.logs / "compile.process.log",
            reporter=reporter,
            command_runner=command_runner,
        )
        _keep_registry_candidates(runtime, run)
        completed("compile")

        packages = {"jdf": run.partial_release / "jdf", "czptt": run.partial_release / "czptt"}
        _check_stops(packages, fetched.czptt, reporter)
        summaries = _package_summaries(packages)
        completed("validation")

        write_json(
            run.partial_release / "release.json",
            _release_record(options, run, fetched, summaries, stages),
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
    command.add_argument("--progress", choices=("auto", "rich", "plain", "off"), default="auto")
    command.add_argument(
        "--keep-work",
        action="store_true",
        help="keep the run's source snapshots after publication",
    )
    command.add_argument("--estimated-posts", action="store_true")
    command.add_argument("--post-inference-policy", type=Path)
    command.add_argument("--refresh-osm", action="store_true")
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
    command.add_argument(
        "--no-route-shapes",
        action="store_true",
        help="build without generated route shapes (BASE_PLAN.md section 17)",
    )
    command.add_argument(
        "--run-id",
        type=parse_run_id,
        help="use this run id instead of a fresh one (CI reserves its release tag up front)",
    )
    realtime_commands.add_parsers(commands)
    release_commands.add_parsers(commands)
    api_commands.add_parsers(commands)
    reference_commands.add_parsers(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in ("rt", "jobs"):
        return realtime_commands.run(args)
    if args.command in ("db", "release"):
        return release_commands.run(args)
    if args.command == "api":
        return api_commands.run(args)
    if args.command == "ref":
        return reference_commands.run(args)
    try:
        runtime = load_runtime_config(cast(Path | None, args.config))
        requested_year = cast(int | Literal["auto"], args.gvd_year)
        gvd_year = gvd.resolve_timetable_year(requested_year)
        result = build(
            BuildOptions(
                runtime=runtime,
                gvd_year=gvd_year,
                jobs=cast(JobSetting, args.jobs),
                progress=cast(ProgressMode, args.progress),
                keep_work=cast(bool, args.keep_work),
                estimated_posts=cast(bool, args.estimated_posts),
                post_inference_policy=cast(Path | None, args.post_inference_policy),
                refresh_osm=cast(bool, args.refresh_osm),
                filtered_jdf=not cast(bool, args.skip_filtered_jdf),
                routing_cache=not cast(bool, args.no_routing_cache),
                czptt_source_cache=not cast(bool, args.no_czptt_source_cache),
                route_shapes=not cast(bool, args.no_route_shapes),
                run_id=cast(str | None, args.run_id),
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
