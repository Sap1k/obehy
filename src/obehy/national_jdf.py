"""Download, fix, merge, and bundle the national municipal/road JDF feeds."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from obehy.gvd import prague_today, resolve_timetable_year
from obehy.osm_snapshot import (
    OsmSnapshotError,
    prepare_jdf_demand_routing,
    validate_jdf_post_candidates,
    validate_snapshot,
)
from obehy.pipeline import jrutil
from obehy.pipeline.args import JobSetting, parse_jobs, parse_memory_budget
from obehy.pipeline.download import DownloadFn, DownloadRecord, download_file
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import (
    ZIP_COMPRESSION_LEVELS,
    ArtifactIdentity,
    ZipCompression,
    deterministic_zip,
    file_digest,
    utc_now,
    write_json,
)
from obehy.pipeline.process import (
    CommandFn,
    CommandResult,
    command_manifest,
    failure_record,
    report_failure,
    run_command,
)
from obehy.pipeline.reporting import (
    BuildReporter,
    CommandProgress,
    ProgressMode,
    Reporter,
    StageClock,
)
from obehy.pipeline.staging import create as create_staging
from obehy.production_package import ProductionPackageError, extracted_gtfs, read_manifest
from obehy.runtime_config import ConfigurationError, load_runtime_config

VLD_URL = "https://portal.cisjr.cz/pub/JDF/JDF.zip"
DRAHY_URL = "https://portal.cisjr.cz/pub/draha/mestske/JDF.zip"
TRANSPORT_MODE_RULES = Path(__file__).with_name("data") / "jdf_transport_mode_rules.csv"
DEFAULT_POST_INFERENCE_POLICY = (
    Path(__file__).with_name("data") / "post-inference" / "learned-v1.json"
)


@dataclass(frozen=True)
class BuildConfig:
    output: Path
    workdir: Path
    osm_file: Path
    jrutil_root: Path | None
    jrutil_command: tuple[str, ...] | None
    geodata_root: Path
    keep_work: bool = False
    progress: ProgressMode = "auto"
    jobs: JobSetting = "auto"
    fix_jobs: JobSetting | None = None
    merge_jobs: JobSetting | None = None
    memory_budget: str = "auto"
    zip_compression: ZipCompression = "balanced"
    estimated_posts: bool = False
    post_inference_policy: Path | None = None
    capture_post_inference_evidence: bool = False
    build_jrutil: bool = True
    # Timetables expired before reference_date or outside GVD gvd_year are dropped;
    # None resolves to today in Europe/Prague and its GVD.
    gvd_year: int | None = None
    reference_date: date | None = None


@dataclass(frozen=True)
class BatchMapping:
    source: str
    original_path: str
    combined_filename: str


def _validated_zip_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    seen: set[str] = set()
    entries: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        normalized = info.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise PipelineError(f"Unsafe ZIP entry: {info.filename}")
        key = normalized.casefold().rstrip("/")
        if key in seen:
            raise PipelineError(f"Case-insensitive duplicate ZIP entry: {info.filename}")
        seen.add(key)
        unix_mode = info.external_attr >> 16
        if stat.S_ISLNK(unix_mode):
            raise PipelineError(f"Symbolic links are not accepted in ZIP files: {info.filename}")
        entries.append(info)
    return entries


def stage_nested_jdf_batches(
    sources: Sequence[tuple[str, Path]],
    destination: Path,
    reporter: Reporter | None = None,
) -> list[BatchMapping]:
    """Stream validated nested JDF ZIPs directly into the combined batch root."""

    destination.mkdir(parents=True, exist_ok=False)
    planned: list[tuple[str, Path, str, str]] = []
    combined_names: set[str] = set()
    for source_name, archive_path in sources:
        source_stems: dict[str, str] = {}
        with zipfile.ZipFile(archive_path) as archive:
            entries = sorted(
                (
                    info
                    for info in _validated_zip_entries(archive)
                    if not info.is_dir()
                    and PurePosixPath(info.filename.replace("\\", "/")).suffix.casefold() == ".zip"
                ),
                key=lambda info: info.filename.replace("\\", "/"),
            )
        if not entries:
            raise PipelineError(
                f"Downloaded archive contains no nested JDF batches: {archive_path}"
            )
        for info in entries:
            original = info.filename.replace("\\", "/")
            stem = PurePosixPath(original).stem
            stem_key = stem.casefold()
            if previous := source_stems.get(stem_key):
                raise PipelineError(
                    f"Duplicate JDF batch basename in {source_name}: {previous} and {original}"
                )
            source_stems[stem_key] = original
            combined_name = f"{source_name}-{stem}.zip"
            combined_key = combined_name.casefold()
            if combined_key in combined_names:
                raise PipelineError(
                    f"Case-insensitive combined JDF batch collision: {combined_name}"
                )
            combined_names.add(combined_key)
            planned.append((source_name, archive_path, info.filename, combined_name))

    task = (
        reporter.start("Stage national batches", total=len(planned), unit="batches")
        if reporter
        else None
    )
    mappings: list[BatchMapping] = []
    open_archives: dict[Path, zipfile.ZipFile] = {}
    try:
        for source_name, archive_path, original_name, combined_name in planned:
            archive = open_archives.get(archive_path)
            if archive is None:
                archive = zipfile.ZipFile(archive_path)
                open_archives[archive_path] = archive
            target = destination / combined_name
            temporary = target.with_suffix(target.suffix + ".part")
            info = archive.getinfo(original_name)
            with archive.open(info) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            try:
                with zipfile.ZipFile(temporary) as nested:
                    nested_entries = _validated_zip_entries(nested)
            except zipfile.BadZipFile as error:
                raise PipelineError(
                    f"Malformed nested JDF ZIP: {archive_path}!{original_name}"
                ) from error
            versions = [
                entry
                for entry in nested_entries
                if PurePosixPath(entry.filename.replace("\\", "/")).name.casefold()
                == "verzejdf.txt"
            ]
            if len(versions) != 1:
                raise PipelineError(
                    f"JDF ZIP must contain exactly one VerzeJDF.txt: {archive_path}!{original_name}"
                )
            os.replace(temporary, target)
            mappings.append(
                BatchMapping(
                    source=source_name,
                    original_path=original_name.replace("\\", "/"),
                    combined_filename=combined_name,
                )
            )
            if reporter is not None and task is not None:
                reporter.update(task, advance=1, detail=combined_name)
    finally:
        for archive in open_archives.values():
            archive.close()
    if reporter is not None and task is not None:
        reporter.finish(task, f"{len(mappings)} batches")
    return mappings


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


def _multitool_command(config: BuildConfig, arguments: Sequence[str]) -> list[str]:
    return [*jrutil.runtime_command(config.jrutil_root, config.jrutil_command), *arguments]


def _jrutil_cwd(config: BuildConfig) -> Path:
    return config.jrutil_root or config.workdir


def _job_text(value: JobSetting) -> str:
    return str(value)


def _stage_jobs(config: BuildConfig, stage: Literal["fix", "merge"]) -> JobSetting:
    override = config.fix_jobs if stage == "fix" else config.merge_jobs
    return config.jobs if override is None else override


def _validate_build_config(config: BuildConfig) -> None:
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
        raise PipelineError(f"JrUtil directory does not exist: {config.jrutil_root}")
    if not config.geodata_root.is_dir():
        raise PipelineError(f"Geodata directory does not exist: {config.geodata_root}")
    for name, value in (
        ("jobs", config.jobs),
        ("fix_jobs", config.fix_jobs),
        ("merge_jobs", config.merge_jobs),
    ):
        if (
            value is not None
            and value != "auto"
            and (not isinstance(value, int) or isinstance(value, bool) or value <= 0)
        ):
            raise PipelineError(f"{name} must be 'auto' or a positive integer")
    if not re.fullmatch(
        r"(?i)(?:auto|[0-9]+(?:\.[0-9]+)?(?:KiB|MiB|GiB))",
        config.memory_budget,
    ):
        raise PipelineError("memory_budget must be 'auto' or a size such as 10GiB")
    if config.zip_compression not in ZIP_COMPRESSION_LEVELS:
        raise PipelineError("zip_compression must be one of: " + ", ".join(ZIP_COMPRESSION_LEVELS))
    if config.capture_post_inference_evidence and config.post_inference_policy is not None:
        raise PipelineError("capture-only post inference cannot be combined with a policy")
    if config.post_inference_policy is not None and not config.post_inference_policy.is_file():
        raise PipelineError(f"Post-inference policy does not exist: {config.post_inference_policy}")


def effective_post_inference_policy(config: BuildConfig) -> Path | None:
    """Return the policy JrUtil should infer posts with.

    Estimated posts default to the learned scorer. Capture-only runs take no
    policy, because they record evidence without deciding assignments.
    """
    if config.capture_post_inference_evidence:
        return None
    if config.post_inference_policy is not None:
        return config.post_inference_policy
    if config.estimated_posts:
        return DEFAULT_POST_INFERENCE_POLICY
    return None


def _verify_fixed_batches(fixed_root: Path, expected: set[str]) -> None:
    archives = sorted(fixed_root.glob("*.zip"), key=lambda path: path.name)
    actual = [path.stem for path in archives]
    actual_set = set(actual)
    malformed: list[str] = []
    for archive_path in archives:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = _validated_zip_entries(archive)
        except zipfile.BadZipFile:
            malformed.append(archive_path.name)
            continue
        versions = [
            entry
            for entry in entries
            if PurePosixPath(entry.filename.replace("\\", "/")).name.casefold() == "verzejdf.txt"
        ]
        if len(versions) != 1:
            malformed.append(archive_path.name)
    if malformed or actual_set != expected or len(actual) != len(actual_set):
        raise PipelineError(
            f"Fixed batch accounting mismatch for {fixed_root}: "
            f"missing={sorted(expected - actual_set)}, "
            f"unexpected={sorted(actual_set - expected)}, "
            f"duplicates={sorted(name for name in actual_set if actual.count(name) > 1)}, "
            f"malformed={malformed}"
        )


def _verify_bundle(bundle: Path, reporter: Reporter | None = None) -> dict[str, Any]:
    manifest = read_manifest(bundle)
    conversion = cast(dict[str, object], manifest.get("compiler", {}))
    estimated_posts = cast(dict[str, object], conversion.get("estimated_posts", {}))
    if estimated_posts and not {
        "candidate_bearing_stops",
        "authored_posts_positioned",
        "single_internal_posts",
        "physical_internal_posts",
        "weak_or_unresolved_contexts",
    }.issubset(estimated_posts):
        raise PipelineError("Bundle manifest is missing the estimated-post counters")
    with extracted_gtfs(bundle) as gtfs:
        trips = gtfs / "trips.txt"
        if not trips.is_file() or len(trips.read_text(encoding="utf-8-sig").splitlines()) < 2:
            raise PipelineError("Bundle GTFS contains no trips")
        verify_gtfs_stops(gtfs, reporter)
    return manifest


def _read_post_inference_evidence_manifest(evidence: Path) -> dict[str, Any]:
    """Read the identity of the evidence pack JrUtil captured and validated."""
    manifest_path = evidence / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineError(f"Cannot read JrUtil evidence manifest: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise PipelineError(f"JrUtil evidence manifest is not an object: {manifest_path}")
    return cast(dict[str, Any], manifest)


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


def _bundle_arguments(
    config: BuildConfig,
    *,
    publish: Path,
    logs: Path,
    descriptor_path: Path,
    converter_version: str,
    gvd_year: int,
    routing_osm_file: Path | None,
    merged_zip: Path,
    bundle: Path,
) -> list[str]:
    return [
        "jdf-to-bundle",
        "--progress-events",
        f"--jobs={_job_text(config.jobs)}",
        f"--memory-budget={config.memory_budget}",
        "--international-route-policy=regional-adjacent",
        f"--transport-mode-rules={TRANSPORT_MODE_RULES}",
        f"--snapshot-descriptor={descriptor_path}",
        f"--converter-version={converter_version}",
        f"--gvd-year={gvd_year}",
        *(
            [f"--routing-osm-pbf={routing_osm_file}"]
            if routing_osm_file is not None
            else ["--no-estimated-posts"]
        ),
        *(
            [f"--post-inference-policy={policy.resolve()}"]
            if (policy := effective_post_inference_policy(config)) is not None
            else []
        ),
        *(
            [
                f"--capture-post-inference-evidence={publish / 'post-inference-evidence-v2'}",
                "--post-inference-evidence-only",
            ]
            if config.capture_post_inference_evidence
            else []
        ),
        f"--diagnostics-out={publish / 'diagnostics-detail'}",
        f"--logfile={logs / 'bundle.log'}",
        str(merged_zip),
        str(bundle),
    ]


def _evidence_summary(manifest: dict[str, Any], capture_metrics: object) -> dict[str, object]:
    return {
        "evidence_format": manifest["evidence_format"],
        "schema_version": manifest["schema_version"],
        "router_evidence_version": manifest["router_evidence_version"],
        "variant_enumeration_version": manifest["variant_enumeration_version"],
        "capture_tool_version": manifest["capture_tool_version"],
        "pack_id": manifest["pack_id"],
        "observation_count": manifest["observation_count"],
        "route_point_count": manifest["route_point_count"],
        "context_count": manifest["context_count"],
        "corridor_variant_count": manifest["corridor_variant_count"],
        "route_point_evidence_count": manifest["route_point_evidence_count"],
        "bytes": sum(
            cast(int, entry["bytes"]) for entry in cast(list[dict[str, Any]], manifest["files"])
        ),
        "capture_metrics": capture_metrics,
    }


@dataclass(frozen=True)
class _Plan:
    """Settings of one build derived from its configuration and the prepared OSM."""

    reference_date: date
    gvd_year: int
    inferred_posts: bool
    osm_manifest: Mapping[str, Any]
    jdf_osm_file: Path


@dataclass(frozen=True)
class _Verified:
    """What the verification stage learned about the bundle or the captured evidence pack."""

    bundle_manifest: dict[str, Any] | None
    evidence_manifest: dict[str, Any] | None
    evidence_manifest_sha256: str | None
    capture_metrics: object


def _plan(config: BuildConfig) -> _Plan:
    reference_date = config.reference_date or prague_today()
    gvd_year = config.gvd_year or resolve_timetable_year("auto")
    osm_manifest = validate_snapshot(config.osm_file, config.workdir)
    return _Plan(
        reference_date=reference_date,
        gvd_year=gvd_year,
        inferred_posts=config.estimated_posts or config.capture_post_inference_evidence,
        osm_manifest=osm_manifest,
        jdf_osm_file=validate_jdf_post_candidates(config.workdir, str(osm_manifest["merge_key"])),
    )


def _download_sources(
    download: DownloadFn, sources: Path, reporter: Reporter, clock: StageClock
) -> tuple[DownloadRecord, DownloadRecord]:
    clock.start("download-vld")
    vld = download(VLD_URL, sources / "JDF_VLD.zip", "VLD", reporter)
    clock.start("download-drahy")
    drahy = download(DRAHY_URL, sources / "JDF_drahy.zip", "dráhy", reporter)
    write_json(
        sources / "sources.json",
        {"schema_version": 1, "sources": [asdict(vld), asdict(drahy)]},
    )
    return vld, drahy


def _fix_arguments(
    config: BuildConfig, plan: _Plan, logs: Path, batches: Path, fixed_root: Path
) -> list[str]:
    return [
        "fix-jdf",
        "--strict",
        "--progress-events",
        "--batch-output=zip",
        f"--jobs={_job_text(_stage_jobs(config, 'fix'))}",
        f"--memory-budget={config.memory_budget}",
        "--international-route-policy=regional-adjacent",
        *([] if plan.inferred_posts else ["--no-estimated-posts"]),
        f"--ext-geodata={config.geodata_root}",
        f"--cz-pbf={plan.jdf_osm_file}",
        f"--logfile={logs / 'fix.log'}",
        str(batches),
        str(fixed_root),
    ]


def _merge_arguments(
    config: BuildConfig, plan: _Plan, logs: Path, merged_directory: Path, fixed_root: Path
) -> list[str]:
    return [
        "merge-jdf",
        "--strict",
        f"--gvd-year={plan.gvd_year}",
        f"--reference-date={plan.reference_date.isoformat()}",
        "--progress-events",
        f"--jobs={_job_text(_stage_jobs(config, 'merge'))}",
        f"--memory-budget={config.memory_budget}",
        f"--logfile={logs / 'merge.log'}",
        str(merged_directory),
        str(fixed_root),
    ]


def _snapshot_descriptor(
    vld: DownloadRecord, drahy: DownloadRecord, merged: ArtifactIdentity
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_id": "national-jdf-vld-drahy",
        "retrieved_at": max(vld.retrieved_at, drahy.retrieved_at),
        "retrieval_method": "derived-from-https-and-configured-snapshots",
        "source_uri": "obehy:derived:national-jdf-vld-drahy",
        "licence": "CIS JŘ public data; OSM ODbL; external geodata source-specific",
        "payload_kind": "zip",
        "payload_sha256": merged.sha256,
        "payload_bytes": merged.bytes,
    }


def _verify(
    config: BuildConfig,
    publish: Path,
    bundle: Path,
    run_jrutil: Callable[[str, list[str], str, CommandProgress], None],
    bundle_result: CommandResult | None,
    reporter: Reporter,
) -> _Verified:
    """Validate the published bundle, or read the captured evidence pack instead of one."""
    evidence_path = (
        publish / "post-inference-evidence-v2" if config.capture_post_inference_evidence else None
    )
    evidence_manifest = (
        _read_post_inference_evidence_manifest(evidence_path) if evidence_path is not None else None
    )
    bundle_manifest = (
        None if config.capture_post_inference_evidence else _verify_bundle(bundle, reporter)
    )
    if bundle_manifest is not None:
        run_jrutil(
            "validate_package",
            ["validate-package", str(bundle)],
            "validate-package.process.log",
            CommandProgress("Validate production package", stage="validate-package"),
        )
    return _Verified(
        bundle_manifest=bundle_manifest,
        evidence_manifest=evidence_manifest,
        evidence_manifest_sha256=(
            file_digest(evidence_path / "manifest.json") if evidence_path is not None else None
        ),
        capture_metrics=(
            bundle_result.capture_metrics
            if config.capture_post_inference_evidence and bundle_result is not None
            else None
        ),
    )


def _extract_manifest(path: Path, extra: Mapping[str, object] | None = None) -> dict[str, object]:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": file_digest(path),
        **(extra or {}),
    }


def _conversion_manifest(config: BuildConfig, plan: _Plan) -> dict[str, object]:
    return {
        "gvd_year": plan.gvd_year,
        "reference_date": plan.reference_date.isoformat(),
        "stop_merge": "name",
        "strict": True,
        "international_route_policy": "regional-adjacent",
        "transport_mode_rules": {
            "path": "obehy/data/jdf_transport_mode_rules.csv",
            "sha256": file_digest(TRANSPORT_MODE_RULES),
        },
        "estimated_posts": plan.inferred_posts,
        "post_inference_policy": (
            str(policy) if (policy := effective_post_inference_policy(config)) is not None else None
        ),
        "capture_post_inference_evidence": config.capture_post_inference_evidence,
    }


def _run_manifest(
    config: BuildConfig,
    plan: _Plan,
    *,
    sources: Path,
    bundle: Path,
    routing_osm_file: Path | None,
    geodata: dict[str, Any],
    jrutil_identity: dict[str, Any],
    command_results: Mapping[str, CommandResult | None],
    clock: StageClock,
    mappings: Sequence[BatchMapping],
    merged_directory: Path,
    merged: ArtifactIdentity,
    verified: _Verified,
) -> dict[str, object]:
    evidence = verified.evidence_manifest
    return {
        "schema_version": 1,
        "completed_at": utc_now(),
        "sources_manifest_sha256": file_digest(sources / "sources.json"),
        "osm_source_key": plan.osm_manifest["merge_key"],
        "osm_jdf_transit_extract": _extract_manifest(plan.jdf_osm_file),
        "osm_jdf_routing_extract": (
            _extract_manifest(
                routing_osm_file,
                {
                    "manifest": str(
                        routing_osm_file.with_suffix(routing_osm_file.suffix + ".manifest.json")
                    )
                },
            )
            if routing_osm_file is not None
            else None
        ),
        "geodata": geodata,
        "jrutil": jrutil_identity,
        "conversion": _conversion_manifest(config, plan),
        "execution": {
            "requested": {
                "jobs": _job_text(config.jobs),
                "fix_jobs": _job_text(_stage_jobs(config, "fix")),
                "merge_jobs": _job_text(_stage_jobs(config, "merge")),
                "memory_budget": config.memory_budget,
            },
            "commands": {
                name: command_manifest(result) for name, result in command_results.items()
            },
            "stage_timings_seconds": clock.timings(),
        },
        "batch_counts": {
            "vld": sum(mapping.source == "vld" for mapping in mappings),
            "drahy": sum(mapping.source == "drahy" for mapping in mappings),
            "total": len(mappings),
        },
        "batch_mapping": [asdict(mapping) for mapping in mappings],
        "merged_jdf": {
            "bytes": merged.bytes,
            "sha256": merged.sha256,
            "uncompressed_bytes": sum(
                path.stat().st_size for path in merged_directory.rglob("*") if path.is_file()
            ),
            "compression": config.zip_compression,
            "compression_level": ZIP_COMPRESSION_LEVELS[config.zip_compression],
        },
        "bundle_manifest_sha256": (
            None if verified.bundle_manifest is None else file_digest(bundle / "manifest.json")
        ),
        "bundle_file_count": (
            0
            if verified.bundle_manifest is None
            else len(cast(list[object], verified.bundle_manifest["files"]))
        ),
        "post_inference_evidence_manifest_sha256": verified.evidence_manifest_sha256,
        "post_inference_evidence": (
            _evidence_summary(evidence, verified.capture_metrics) if evidence is not None else None
        ),
    }


def build(
    config: BuildConfig,
    download: DownloadFn = download_file,
    command_runner: CommandFn = run_command,
    reporter: Reporter | None = None,
) -> Path:
    _validate_build_config(config)
    plan = _plan(config)
    staging = create_staging(config.output, config.workdir, "national-jdf")
    publish = staging.publish
    sources = publish / "sources"
    derived = publish / "derived"
    bundle = publish / "bundle"
    logs = publish / "logs"
    work = staging.work
    for directory in (sources, derived, logs):
        directory.mkdir(parents=True)
    clock = StageClock()
    command_results: dict[str, CommandResult | None] = {}

    owned_reporter = reporter is None
    reporter = reporter or BuildReporter(config.progress)

    def run_jrutil(
        name: str, arguments: list[str], log_name: str, progress: CommandProgress
    ) -> None:
        command_results[name] = command_runner(
            _multitool_command(config, arguments),
            _jrutil_cwd(config),
            logs / log_name,
            reporter,
            progress,
        )

    reporter.note(
        "Build configuration: "
        f"fix jobs={_job_text(_stage_jobs(config, 'fix'))}, "
        f"merge jobs={_job_text(_stage_jobs(config, 'merge'))}, "
        f"memory budget={config.memory_budget}, "
        f"ZIP compression={config.zip_compression} "
        f"(level {ZIP_COMPRESSION_LEVELS[config.zip_compression]}), "
        f"run={staging.run_root}, staging={staging.stage}, output={staging.output}"
    )
    try:
        vld, drahy = _download_sources(download, sources, reporter, clock)

        clock.start("stage-national-batches")
        batches = work / "batches"
        mappings = stage_nested_jdf_batches(
            (
                ("vld", sources / "JDF_VLD.zip"),
                ("drahy", sources / "JDF_drahy.zip"),
            ),
            batches,
            reporter,
        )

        clock.start("provenance")
        jrutil_identity = jrutil.provenance(config.jrutil_root, config.jrutil_command)
        geodata = geodata_manifest(config.geodata_root)
        if not TRANSPORT_MODE_RULES.is_file():
            raise PipelineError(f"Transport mode rules are missing: {TRANSPORT_MODE_RULES}")

        clock.start("build-jrutil")
        build_command = (
            None if config.jrutil_root is None else jrutil.build_command(config.jrutil_root)
        )
        if config.build_jrutil and build_command is not None:
            command_results["build"] = command_runner(
                build_command,
                _jrutil_cwd(config),
                logs / "jrutil-build.process.log",
                reporter,
                CommandProgress("Build JrUtil", stage="build-jrutil"),
            )

        clock.start("fix-national-jdf")
        fixed_root = work / "fixed"
        run_jrutil(
            "fix",
            _fix_arguments(config, plan, logs, batches, fixed_root),
            "fix.process.log",
            CommandProgress(
                "Fix national JDF",
                total=len(mappings),
                event="Completed JDF batch",
                stage="fix-jdf",
            ),
        )
        _verify_fixed_batches(fixed_root, {Path(item.combined_filename).stem for item in mappings})

        clock.start("merge-national-jdf")
        merged_directory = work / "merged-jdf"
        run_jrutil(
            "merge",
            _merge_arguments(config, plan, logs, merged_directory, fixed_root),
            "merge.process.log",
            CommandProgress(
                "Merge national JDF",
                total=len(mappings),
                event="Completed merge of JDF batch",
                stage="merge-jdf",
            ),
        )
        routing_osm_file: Path | None = None
        if plan.inferred_posts:
            clock.start("prepare-routing-osm")
            routing_osm_file = prepare_jdf_demand_routing(
                config.workdir,
                merged_directory / "JrutilRoutingDemands.txt",
                str(plan.osm_manifest["merge_key"]),
            )

        clock.start("package-merged-jdf")
        merged_zip = derived / "merged-jdf.zip"
        merged = deterministic_zip(
            merged_directory,
            merged_zip,
            reporter,
            compression_level=ZIP_COMPRESSION_LEVELS[config.zip_compression],
        )
        descriptor_path = derived / "snapshot-descriptor.json"
        write_json(descriptor_path, _snapshot_descriptor(vld, drahy, merged))

        clock.start("generate-bundle")
        run_jrutil(
            "bundle",
            _bundle_arguments(
                config,
                publish=publish,
                logs=logs,
                descriptor_path=descriptor_path,
                converter_version=jrutil.converter_version(jrutil_identity),
                gvd_year=plan.gvd_year,
                routing_osm_file=routing_osm_file,
                merged_zip=merged_zip,
                bundle=bundle,
            ),
            "bundle.process.log",
            CommandProgress("Generate GTFS + Parquet bundle", stage="jdf-to-bundle"),
        )

        clock.start(
            "record-evidence" if config.capture_post_inference_evidence else "validate-bundle"
        )
        verified = _verify(
            config, publish, bundle, run_jrutil, command_results.get("bundle"), reporter
        )

        clock.start("write-run-manifest")
        write_json(
            publish / "run-manifest.json",
            _run_manifest(
                config,
                plan,
                sources=sources,
                bundle=bundle,
                routing_osm_file=routing_osm_file,
                geodata=geodata,
                jrutil_identity=jrutil_identity,
                command_results=command_results,
                clock=clock,
                mappings=mappings,
                merged_directory=merged_directory,
                merged=merged,
                verified=verified,
            ),
        )
        clock.start("activation")
        return staging.activate(reporter, keep_work=config.keep_work)
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
        if owned_reporter:
            reporter.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obehy-national-jdf")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build a national JDF conversion bundle")
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--config", type=Path)
    build_parser.add_argument("--keep-work", action="store_true")
    build_parser.add_argument(
        "--jobs",
        type=parse_jobs,
        default="auto",
        help="workers for parallel JrUtil stages: auto or a positive integer",
    )
    build_parser.add_argument(
        "--fix-jobs",
        type=parse_jobs,
        help="override --jobs for fix-jdf",
    )
    build_parser.add_argument(
        "--merge-jobs",
        type=parse_jobs,
        help="override --jobs for merge-jdf",
    )
    build_parser.add_argument(
        "--memory-budget",
        type=parse_memory_budget,
        default="auto",
        help="JrUtil parallel-work budget such as 10GiB or auto",
    )
    build_parser.add_argument(
        "--zip-compression",
        choices=("fast", "balanced", "small"),
        default="balanced",
        help="merged-JDF ZIP tradeoff: fast=1, balanced=6, small=9",
    )
    build_parser.add_argument(
        "--progress",
        choices=("auto", "rich", "plain", "off"),
        default="auto",
        help="terminal progress mode (default: auto)",
    )
    build_parser.add_argument(
        "--estimated-posts",
        action="store_true",
        help="enable conservative inference and build the demand-clipped routing input",
    )
    build_parser.add_argument(
        "--post-inference-policy",
        type=Path,
        help="post-inference policy (default: the packaged learned-v1 scorer)",
    )
    build_parser.add_argument(
        "--capture-post-inference-evidence",
        action="store_true",
        help="publish a national evidence-v2 pack and run manifest instead of a bundle",
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
            jrutil_root=runtime.jrutil.directory,
            jrutil_command=runtime.jrutil.command,
            geodata_root=runtime.jrunify_ext_geodata_dir / "other",
            keep_work=cast(bool, args.keep_work),
            progress=cast(ProgressMode, args.progress),
            jobs=cast(JobSetting, args.jobs),
            fix_jobs=cast(JobSetting | None, args.fix_jobs),
            merge_jobs=cast(JobSetting | None, args.merge_jobs),
            memory_budget=cast(str, args.memory_budget),
            zip_compression=cast(ZipCompression, args.zip_compression),
            estimated_posts=cast(bool, args.estimated_posts),
            post_inference_policy=cast(Path | None, args.post_inference_policy),
            capture_post_inference_evidence=cast(bool, args.capture_post_inference_evidence),
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
    print(f"National JDF bundle written to {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
