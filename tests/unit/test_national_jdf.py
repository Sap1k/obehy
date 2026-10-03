from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import pytest

from obehy import national_jdf
from obehy.national_jdf import BuildConfig, build, stage_nested_jdf_batches
from obehy.pipeline import download, jrutil, reporting
from obehy.pipeline.download import DownloadRecord, download_file
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import deterministic_zip, file_digest
from obehy.pipeline.process import CommandFailure, CommandResult, run_command
from obehy.pipeline.reporting import BuildReporter, CommandProgress


def test_transport_mode_rules_exclude_liberec_replacement_buses() -> None:
    with national_jdf.TRANSPORT_MODE_RULES.open(encoding="utf-8", newline="") as stream:
        rules = list(csv.DictReader(stream))

    liberec_routes = {row["route_id_from"] for row in rules if row["agency_id"] == "47311975"}
    assert liberec_routes == {"545002", "545003", "545004", "545005", "545011"}
    assert {"545902", "545903"}.isdisjoint(liberec_routes)


def test_post_evidence_manifest_is_read_without_parquet_rescan(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = {
        "pack_id": "pack",
        "capture_tool_version": "fixture-tool",
        "merged_jdf_sha256": "0" * 64,
        "routing_pbf_sha256": "1" * 64,
        "osm_snapshot": None,
        "router_evidence_version": "packed-directed-v3",
        "variant_enumeration_version": "directed-thread-top3-v1",
        "capture_ceilings": {
            "routed_excess_metres": 1000.0,
            "maximum_corridor_variants": 3,
        },
        "maximum_search_states": 100_000,
        "maximum_search_distance_metres": 30_000.0,
        "files": [{"path": "route_point_evidence.parquet", "rows": 35_540_192}],
    }
    manifest_path = evidence / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = national_jdf._read_post_inference_evidence_manifest(  # pyright: ignore[reportPrivateUsage]
        evidence
    )

    assert loaded == manifest


def test_capture_only_rejects_post_inference_policy(tmp_path: Path) -> None:
    geodata = tmp_path / "geodata"
    geodata.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    config = BuildConfig(
        output=tmp_path / "output",
        workdir=tmp_path / "work",
        osm_file=tmp_path / "cz.osm.pbf",
        jrutil_root=None,
        jrutil_command=("jrutil",),
        geodata_root=geodata,
        post_inference_policy=policy,
        capture_post_inference_evidence=True,
    )

    with pytest.raises(PipelineError, match=r"capture-only.*policy"):
        build(config)


def test_estimated_posts_default_to_learned_policy_except_capture(tmp_path: Path) -> None:
    base = BuildConfig(
        output=tmp_path / "output",
        workdir=tmp_path / "work",
        osm_file=tmp_path / "cz.osm.pbf",
        jrutil_root=None,
        jrutil_command=("jrutil",),
        geodata_root=tmp_path / "geodata",
    )

    assert national_jdf.DEFAULT_POST_INFERENCE_POLICY.is_file()
    assert national_jdf.effective_post_inference_policy(base) is None
    estimated = replace(base, estimated_posts=True)
    assert national_jdf.effective_post_inference_policy(estimated) == (
        national_jdf.DEFAULT_POST_INFERENCE_POLICY
    )
    captured = replace(estimated, capture_post_inference_evidence=True)
    assert national_jdf.effective_post_inference_policy(captured) is None


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return output.getvalue()


def _download_record(name: str, url: str, destination: Path) -> DownloadRecord:
    return DownloadRecord(
        name=name,
        url=url,
        retrieved_at="2026-07-19T12:00:00+00:00",
        bytes=destination.stat().st_size,
        sha256=file_digest(destination),
        etag='"fixture"',
        last_modified="Sun, 19 Jul 2026 12:00:00 GMT",
    )


def test_stage_batches_rejects_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    archive.write_bytes(_zip_bytes({"../outside.zip": b"bad"}))

    with pytest.raises(PipelineError, match="Unsafe ZIP entry"):
        stage_nested_jdf_batches((("vld", archive),), tmp_path / "output")

    assert not (tmp_path / "outside.zip").exists()


def test_stage_batches_rejects_duplicate_line_local_archive_names(tmp_path: Path) -> None:
    payload = _zip_bytes({"VerzeJDF.txt": b'"1.11";\r\n'})
    archive = tmp_path / "nested.zip"
    archive.write_bytes(_zip_bytes({"a/1.zip": payload, "b/1.ZIP": payload}))

    with pytest.raises(PipelineError, match="Duplicate JDF batch basename"):
        stage_nested_jdf_batches((("vld", archive),), tmp_path / "output")


def test_deterministic_zip_is_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "z.txt").write_text("z", encoding="utf-8")
    (source / "a.txt").write_text("a", encoding="utf-8")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    deterministic_zip(source, first)
    deterministic_zip(source, second)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["a.txt", "z.txt"]


def test_deterministic_zip_presets_are_distinct_and_report_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.txt").write_bytes(b"national-jdf-row\r\n" * 100_000)
    fast = tmp_path / "fast.zip"
    balanced = tmp_path / "balanced.zip"
    small = tmp_path / "small.zip"

    fast_identity = deterministic_zip(source, fast, compression_level=1)
    balanced_identity = deterministic_zip(source, balanced, compression_level=6)
    small_identity = deterministic_zip(source, small, compression_level=9)

    assert fast_identity.bytes == fast.stat().st_size
    assert balanced_identity.sha256 == file_digest(balanced)
    assert small_identity.bytes <= balanced_identity.bytes <= fast_identity.bytes


def test_stage_nested_batches_streams_directly_with_stable_mapping(tmp_path: Path) -> None:
    first = _zip_bytes({"VerzeJDF.txt": b'"1.11";\r\n', "Linky.txt": b"one"})
    second = _zip_bytes({"VerzeJDF.txt": b'"1.11";\r\n', "Linky.txt": b"two"})
    vld = tmp_path / "vld.zip"
    drahy = tmp_path / "drahy.zip"
    vld.write_bytes(_zip_bytes({"nested/2.zip": second, "1.zip": first, "README": b"x"}))
    drahy.write_bytes(_zip_bytes({"1.zip": second}))

    mappings = stage_nested_jdf_batches(
        (("vld", vld), ("drahy", drahy)),
        tmp_path / "batches",
    )

    assert [mapping.combined_filename for mapping in mappings] == [
        "vld-1.zip",
        "vld-2.zip",
        "drahy-1.zip",
    ]
    assert [mapping.original_path for mapping in mappings] == [
        "1.zip",
        "nested/2.zip",
        "1.zip",
    ]
    assert vld.is_file() and drahy.is_file()
    with zipfile.ZipFile(tmp_path / "batches" / "vld-1.zip") as archive:
        assert archive.read("Linky.txt") == b"one"


def test_stage_nested_batches_rejects_malformed_inner_archive(tmp_path: Path) -> None:
    outer = tmp_path / "outer.zip"
    outer.write_bytes(_zip_bytes({"bad.zip": b"not a ZIP"}))

    with pytest.raises(PipelineError, match="Malformed nested JDF ZIP"):
        stage_nested_jdf_batches((("vld", outer),), tmp_path / "batches")

    assert (tmp_path / "batches" / "vld-bad.zip.part").is_file()


@pytest.mark.parametrize("keep_work", [False, True])
@pytest.mark.parametrize("estimated_posts", [False, True])
def test_build_orchestrates_fix_merge_and_bundle_atomically(
    tmp_path: Path,
    keep_work: bool,
    estimated_posts: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "national"
    jrutil_root = tmp_path / "jrutil"
    project = jrutil_root / "jrutil-multitool" / "jrutil-multitool.fsproj"
    project.parent.mkdir(parents=True)
    project.write_text("<Project />\n", encoding="utf-8")
    geodata_root = tmp_path / "jrunify-ext-geodata" / "other"
    geodata_root.mkdir(parents=True)
    (geodata_root / "fixture.csv").write_text("Town,Stop,49.0,14.0,CZ\n", encoding="utf-8")

    def fake_git_identity(_repository: Path) -> dict[str, object]:
        return {
            "commit": "0123456789abcdef0123456789abcdef01234567",
            "dirty": False,
            "status": [],
        }

    monkeypatch.setattr(jrutil, "git_identity", fake_git_identity)

    def valid_snapshot(_osm: Path, _workdir: Path) -> dict[str, object]:
        return {"merge_key": "fixture-osm"}

    monkeypatch.setattr(
        national_jdf,
        "validate_snapshot",
        valid_snapshot,
    )
    transit_extract = tmp_path / "workdir" / "osm" / "jdf-transit-geometry.osm.pbf"

    def valid_transit(_workdir: Path, source_key: str) -> Path:
        assert source_key == "fixture-osm"
        transit_extract.parent.mkdir(parents=True, exist_ok=True)
        transit_extract.write_bytes(b"transit-osm")
        return transit_extract

    monkeypatch.setattr(national_jdf, "validate_jdf_post_candidates", valid_transit)
    routing_extract = tmp_path / "workdir" / "osm" / "jdf-transit-routing-demand.osm.pbf"

    def prepare_routing(_workdir: Path, demands: Path, source_key: str) -> Path:
        assert source_key == "fixture-osm"
        assert demands.name == "JrutilRoutingDemands.txt"
        routing_extract.parent.mkdir(parents=True, exist_ok=True)
        routing_extract.write_bytes(b"routing-osm")
        return routing_extract

    monkeypatch.setattr(national_jdf, "prepare_jdf_demand_routing", prepare_routing)
    commands: list[list[str]] = []
    download_names: list[str] = []

    def fake_download(
        url: str, destination: Path, name: str, _reporter: object = None
    ) -> DownloadRecord:
        download_names.append(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        inner = _zip_bytes({"VerzeJDF.txt": b'"1.11";\r\n'})
        destination.write_bytes(_zip_bytes({"1.zip": inner}))
        return _download_record(name, url, destination)

    def fake_command(
        command: Sequence[str],
        _cwd: Path,
        log: Path,
        _reporter: object = None,
        _progress: object = None,
    ) -> CommandResult | None:
        command = list(command)
        commands.append(command)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fixture command\n", encoding="utf-8")
        if command[1] == "build":
            return
        if "validate-package" in command:
            return
        arguments = command[2:]  # dotnet <multitool.dll> <arguments>
        operation = arguments[0]
        if operation == "fix-jdf":
            input_root = Path(arguments[-2])
            output_root = Path(arguments[-1])
            for archive in input_root.rglob("*.zip"):
                output_root.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(output_root / archive.name, "w") as fixed:
                    fixed.writestr("VerzeJDF.txt", '"1.11";\r\n'.encode("cp1250"))
        elif operation == "merge-jdf":
            merged = Path(arguments[-2])
            merged.mkdir(parents=True)
            (merged / "VerzeJDF.txt").write_text('"1.11";\r\n', encoding="cp1250")
            (merged / "JrutilRoutingDemands.txt").write_text("", encoding="cp1250")
            return CommandResult(
                elapsed_seconds=1.5,
                completed=2,
                total=2,
                execution_plan={"resolved_workers": 2},
                failed_batch=None,
                maximum_in_flight=2,
                resource_usage=(
                    {
                        "event": "resource_usage",
                        "stage": "merge-jdf",
                        "phase": "write-merged-jdf",
                        "peak_working_set_bytes": 123456,
                        "spill_bytes": 654321,
                    },
                ),
            )
        elif operation == "jdf-to-bundle":
            bundle = Path(arguments[-1])
            bundle.mkdir(parents=True)
            with zipfile.ZipFile(bundle / "gtfs.zip", "w") as gtfs:
                gtfs.writestr("trips.txt", "route_id,service_id,trip_id\nr,s,t\n")
                gtfs.writestr(
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                    "s,Stop,50,14,0,p\n"
                    "p,Station,50,14,1,\n",
                )
                gtfs.writestr(
                    "stop_times.txt",
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "t,08:00:00,08:00:00,s,1\n",
                )
            (bundle / "serving").mkdir()
            diagnostics: dict[str, object] = {"schema_version": 1, "diagnostics": []}
            national_jdf.write_json(bundle / "diagnostics.json", diagnostics)
            manifest: dict[str, object] = {
                "bundle_format": "jrutil-production",
                "bundle_version": 3,
                "serving_schema_version": 4,
                "contract_valid": True,
                "publication_eligible": True,
                "compiler": {
                    "estimated_posts": {
                        "candidate_bearing_stops": 0,
                        "authored_posts_positioned": 0,
                        "single_internal_posts": 0,
                        "physical_internal_posts": 0,
                        "weak_or_unresolved_contexts": 0,
                    },
                },
                "files": [],
            }
            national_jdf.write_json(bundle / "manifest.json", manifest)
        else:
            raise AssertionError(f"Unexpected operation: {operation}")

    result = build(
        BuildConfig(
            output=output,
            workdir=tmp_path / "workdir",
            osm_file=tmp_path / "osm" / "regional.osm.pbf",
            jrutil_root=jrutil_root,
            jrutil_command=None,
            geodata_root=geodata_root,
            progress="off",
            keep_work=keep_work,
            estimated_posts=estimated_posts,
            routing_cache_dir=tmp_path / "workdir" / "cache" / "routing",
            gvd_year=2026,
            reference_date=date(2026, 9, 29),
        ),
        fake_download,
        fake_command,
    )

    assert result == output
    assert download_names == ["VLD", "dráhy"]
    assert (output / "derived" / "merged-jdf.zip").is_file()
    assert (output / "bundle" / "manifest.json").is_file()
    assert (output / "work").exists() is keep_work
    if keep_work:
        assert len(list((output / "work" / "fixed").glob("*.zip"))) == 2
    assert commands[0][1] == "build"
    multitool_commands = [command for command in commands if command[1] != "build"]
    operations = [command[2] for command in multitool_commands]
    assert operations == ["fix-jdf", "merge-jdf", "jdf-to-bundle", "validate-package"]
    assert all("--strict" in command for command in multitool_commands[:2])
    assert all("--by-id" not in command for command in multitool_commands)
    assert all("--stop-ids-cis" not in command for command in multitool_commands)
    fix_command, merge_command, bundle_command = multitool_commands[:3]
    assert "--batch-output=zip" in fix_command
    assert "--jobs=auto" in fix_command
    assert "--jobs=auto" in merge_command
    assert "--gvd-year=2026" in merge_command
    assert "--gvd-year=2026" in bundle_command
    assert "--reference-date=2026-09-29" in merge_command
    assert "--memory-budget=auto" in fix_command
    assert "--memory-budget=auto" in merge_command
    assert "--jobs=auto" in bundle_command
    assert "--memory-budget=auto" in bundle_command
    assert "--progress-events" in bundle_command
    assert any(argument.startswith("--ext-geodata=") for argument in fix_command)
    assert any(argument.startswith("--cz-pbf=") for argument in fix_command)
    assert f"--cz-pbf={transit_extract}" in fix_command
    assert ("--no-estimated-posts" in fix_command) is not estimated_posts
    assert not any(argument.startswith("--ext-geodata=") for argument in merge_command)
    assert not any(argument.startswith("--cz-pbf=") for argument in merge_command)
    assert "--international-route-policy=regional-adjacent" in fix_command
    assert "--international-route-policy=regional-adjacent" in bundle_command
    assert any(argument.startswith("--transport-mode-rules=") for argument in bundle_command)
    has_routing_pbf = any(argument.startswith("--routing-osm-pbf=") for argument in bundle_command)
    assert has_routing_pbf is estimated_posts
    assert ("--no-estimated-posts" in bundle_command) is not estimated_posts
    routing_cache = tmp_path / "workdir" / "cache" / "routing"
    assert (f"--routing-cache={routing_cache}" in bundle_command) is estimated_posts
    default_policy = national_jdf.DEFAULT_POST_INFERENCE_POLICY
    assert (
        f"--post-inference-policy={default_policy.resolve()}" in bundle_command
    ) is estimated_posts
    assert all(
        not any(argument.startswith("--cache=") for argument in command)
        for command in multitool_commands
    )
    run_manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["batch_counts"] == {"drahy": 1, "total": 2, "vld": 1}
    assert run_manifest["osm_jdf_transit_extract"] == {
        "path": str(transit_extract),
        "bytes": len(b"transit-osm"),
        "sha256": file_digest(transit_extract),
    }
    assert run_manifest["batch_mapping"] == [
        {"combined_filename": "vld-1.zip", "original_path": "1.zip", "source": "vld"},
        {
            "combined_filename": "drahy-1.zip",
            "original_path": "1.zip",
            "source": "drahy",
        },
    ]
    assert run_manifest["conversion"] == {
        "gvd_year": 2026,
        "reference_date": "2026-09-29",
        "international_route_policy": "regional-adjacent",
        "transport_mode_rules": {
            "path": "obehy/data/jdf_transport_mode_rules.csv",
            "sha256": national_jdf.file_digest(national_jdf.TRANSPORT_MODE_RULES),
        },
        "stop_merge": "name",
        "strict": True,
        "estimated_posts": estimated_posts,
        "post_inference_policy": str(default_policy) if estimated_posts else None,
        "capture_post_inference_evidence": False,
    }
    assert run_manifest["execution"]["requested"] == {
        "fix_jobs": "auto",
        "jobs": "auto",
        "memory_budget": "auto",
        "merge_jobs": "auto",
    }
    assert run_manifest["execution"]["commands"]["merge"]["resource_usage"] == [
        {
            "event": "resource_usage",
            "stage": "merge-jdf",
            "phase": "write-merged-jdf",
            "peak_working_set_bytes": 123456,
            "spill_bytes": 654321,
        }
    ]
    assert run_manifest["merged_jdf"]["compression"] == "balanced"
    assert run_manifest["merged_jdf"]["compression_level"] == 6
    assert run_manifest["jrutil"]["mode"] == "directory"
    assert run_manifest["jrutil"]["git"] == {
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "dirty": False,
        "status": [],
    }
    assert [file["path"] for file in run_manifest["geodata"]["files"]] == ["fixture.csv"]
    if estimated_posts:
        assert run_manifest["osm_jdf_routing_extract"] == {
            "path": str(routing_extract),
            "bytes": len(b"routing-osm"),
            "sha256": file_digest(routing_extract),
            "manifest": str(routing_extract) + ".manifest.json",
        }
        assert run_manifest["routing_cache_dir"] == str(routing_cache)
    else:
        assert run_manifest["osm_jdf_routing_extract"] is None
        assert run_manifest["routing_cache_dir"] is None
    assert run_manifest["post_inference_evidence_manifest_sha256"] is None
    assert run_manifest["post_inference_evidence"] is None
    assert run_manifest["bundle_manifest_sha256"] == file_digest(
        output / "bundle" / "manifest.json"
    )


def test_gtfs_stop_verifier_allows_zero_coordinates_with_aggregate_warning(
    tmp_path: Path,
) -> None:
    (tmp_path / "stops.txt").write_text(
        "stop_id,stop_lat,stop_lon,location_type,parent_station\ns,0,0,0,p\np,50,14,1,\n",
        encoding="utf-8",
    )
    (tmp_path / "stop_times.txt").write_text("trip_id,stop_id\nt,s\n", encoding="utf-8")
    reporter = BuildReporter("off")

    national_jdf.verify_gtfs_stops(tmp_path, reporter)

    assert reporter.snapshot()["problems"] == {"pipeline:warning": 1}


def test_gtfs_stop_verifier_rejects_unreferenced_boarding_stop(tmp_path: Path) -> None:
    (tmp_path / "stops.txt").write_text(
        "stop_id,stop_lat,stop_lon,location_type,parent_station\nused,50,14,0,\norphan,50,14,0,\n",
        encoding="utf-8",
    )
    (tmp_path / "stop_times.txt").write_text("trip_id,stop_id\nt,used\n", encoding="utf-8")

    with pytest.raises(PipelineError, match="unreferenced"):
        national_jdf.verify_gtfs_stops(tmp_path)


def test_build_retains_staging_directory_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "failed-output"
    jrutil_root = tmp_path / "jrutil"
    jrutil_root.mkdir()
    geodata_root = tmp_path / "jrunify-ext-geodata" / "other"
    geodata_root.mkdir(parents=True)

    def failing_download(
        _url: str, _destination: Path, _name: str, _reporter: object = None
    ) -> DownloadRecord:
        raise OSError("fixture download failure")

    def valid_snapshot(_osm: Path, _workdir: Path) -> dict[str, object]:
        return {"merge_key": "fixture-osm"}

    monkeypatch.setattr(
        national_jdf,
        "validate_snapshot",
        valid_snapshot,
    )

    def valid_transit(workdir: Path, source_key: str) -> Path:
        assert source_key == "fixture-osm"
        destination = workdir / "osm" / "jdf-transit-geometry.osm.pbf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"transit-osm")
        return destination

    monkeypatch.setattr(national_jdf, "validate_jdf_post_candidates", valid_transit)
    with pytest.raises(OSError, match="fixture download failure"):
        build(
            BuildConfig(
                output=output,
                workdir=tmp_path / "workdir",
                osm_file=tmp_path / "regional.osm.pbf",
                jrutil_root=jrutil_root,
                jrutil_command=None,
                geodata_root=geodata_root,
                progress="off",
            ),
            failing_download,
        )

    assert not output.exists()
    retained = list(tmp_path.glob(".failed-output.work-*"))
    assert len(retained) == 1
    assert (retained[0] / "publish" / "sources").is_dir()
    failure = json.loads((retained[0] / "failure.json").read_text())
    assert failure["stage"] == "download-vld"
    assert failure["message"] == "fixture download failure"


def test_download_record_json_shape_is_stable(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"data")

    record = _download_record("fixture", "https://example.invalid", payload)

    assert asdict(record)["sha256"] == hashlib.sha256(b"data").hexdigest()


class _Reporter:
    def __init__(self) -> None:
        self.completed = 0
        self.details: list[str] = []
        self.problems: list[tuple[str, str]] = []
        self.notes: list[str] = []

    def stage(self, label: str) -> None:
        del label

    def start(self, label: str, *, total: int | None = None, unit: str = "") -> int:
        del label, total, unit
        return 1

    def update(
        self,
        task: int,
        *,
        advance: int = 0,
        completed: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        del task, total
        self.completed = completed if completed is not None else self.completed + advance
        if detail is not None:
            self.details.append(detail)

    def finish(self, task: int, detail: str = "done") -> None:
        del task, detail

    def problem(self, severity: str, message: str) -> None:
        self.problems.append((severity, message))

    def note(self, message: str) -> None:
        self.notes.append(message)

    def snapshot(self) -> dict[str, object]:
        return {}

    def close(self) -> None: ...


def test_reporter_treats_process_output_as_literal_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = BuildReporter("off")
    process_output = (
        "error MSB4181: The task returned false "
        "[/opt/obehy/jrutil/jrutil-multitool/jrutil-multitool.fsproj]"
    )

    reporter.problem("error", process_output)
    reporter.note("Last process output:\n" + process_output)

    captured = capsys.readouterr().err
    assert captured.count("[/opt/obehy/jrutil/jrutil-multitool/jrutil-multitool.fsproj]") == 2
    assert "Last process output:" in captured


def test_rich_indeterminate_task_gets_a_finished_lifecycle_state() -> None:
    reporter = BuildReporter("rich")
    try:
        task = reporter.start("Build JrUtil")
        reporter.finish(task, "completed")

        progress = reporter._progress  # pyright: ignore[reportPrivateUsage]
        assert progress is not None
        rich_task_id = reporter._rich_tasks[task]  # pyright: ignore[reportPrivateUsage]
        rich_task = progress._tasks[rich_task_id]  # pyright: ignore[reportPrivateUsage]
        assert rich_task.fields["lifecycle_finished"] is True
        assert rich_task.stop_time is not None
        assert rich_task.total is None
        status_column = reporting._LifecycleSpinnerColumn()  # pyright: ignore[reportPrivateUsage]
        rendered_status = status_column.render(rich_task)
        assert str(rendered_status) == "✓"
        bar_column = reporting._LifecycleBarColumn()  # pyright: ignore[reportPrivateUsage]
        rendered_bar = bar_column.render(rich_task)
        assert str(rendered_bar) == ""
    finally:
        reporter.close()


class _Response:
    def __init__(self, chunks: list[bytes], length: int | None = None) -> None:
        self._chunks = iter(chunks)
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def read(self, _size: int = -1) -> bytes:
        return next(self._chunks, b"")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None: ...


@pytest.mark.parametrize("known_size", [True, False])
def test_download_hashes_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, known_size: bool
) -> None:
    chunks = [b"one", b"two", b"three"]
    payload = b"".join(chunks)
    response = _Response(chunks, len(payload) if known_size else None)

    def fake_urlopen(*_args: object, **_kwargs: object) -> _Response:
        return response

    monkeypatch.setattr(download, "urlopen", fake_urlopen)
    reporter = _Reporter()

    record = download_file("https://example.invalid/data", tmp_path / "data", "data", reporter)

    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert record.md5 == hashlib.md5(payload).hexdigest()
    assert reporter.completed == len(payload)


def test_interrupted_download_retains_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Interrupted(_Response):
        def read(self, _size: int = -1) -> bytes:
            chunk = next(self._chunks, None)
            if chunk is None:
                raise OSError("connection lost")
            return chunk

    def interrupted_urlopen(*_args: object, **_kwargs: object) -> Interrupted:
        return Interrupted([b"partial"])

    monkeypatch.setattr(download, "urlopen", interrupted_urlopen)

    with pytest.raises(OSError, match="connection lost"):
        download_file("https://example.invalid/data", tmp_path / "data", "data")

    assert (tmp_path / "data.part").read_bytes() == b"partial"


def test_run_command_tees_raw_output_and_reports_failure(tmp_path: Path) -> None:
    raw = b"[12:00 WRN] warning text\r\nProcessing JDF batch vld-1\r\nstack line\r\n"
    script = "import sys;sys.stdout.buffer.write(" + repr(raw) + ");sys.exit(7)"
    log = tmp_path / "process.log"
    reporter = _Reporter()

    with pytest.raises(CommandFailure) as raised:
        run_command(
            [sys.executable, "-c", script],
            tmp_path,
            log,
            reporter,
            CommandProgress("Fix", total=2, event="Processing JDF batch"),
        )

    assert log.read_bytes() == raw
    assert raised.value.returncode == 7
    assert raised.value.last_batch == "vld-1"
    assert reporter.completed == 1
    assert reporter.problems == [("WRN", "[12:00 WRN] warning text")]


def test_run_command_counts_structured_completions_and_worker_plan(tmp_path: Path) -> None:
    events = [
        {
            "schema_version": 1,
            "event": "execution_plan",
            "stage": "fix-jdf",
            "requested_jobs": "auto",
            "processor_count": 12,
            "memory_budget_bytes": 10 * 1024**3,
            "memory_limited_jobs": 10,
            "resolved_workers": 10,
        },
        {
            "schema_version": 1,
            "event": "batch_started",
            "stage": "fix-jdf",
            "batch": "a.zip",
        },
        {
            "schema_version": 1,
            "event": "batch_started",
            "stage": "fix-jdf",
            "batch": "b.zip",
        },
        {
            "schema_version": 1,
            "event": "batch_completed",
            "stage": "fix-jdf",
            "batch": "b.zip",
        },
        {
            "schema_version": 1,
            "event": "phase",
            "stage": "fix-jdf",
            "name": "write-outputs",
            "state": "started",
        },
        {
            "schema_version": 1,
            "event": "resource_usage",
            "stage": "fix-jdf",
            "phase": "write-outputs",
            "peak_working_set_bytes": 123456,
            "spill_bytes": 654321,
        },
        {
            "schema_version": 1,
            "event": "capture_metrics",
            "stage": "fix-jdf",
            "estimated_evidence_bytes": 1000,
            "atomic_output_headroom_bytes": 2000,
            "current_spill_bytes": 0,
            "peak_spill_bytes": 750,
            "maximum_workers": 4,
        },
        {
            "schema_version": 1,
            "event": "work_progress",
            "stage": "fix-jdf",
            "phase": "routing-contexts",
            "state": "running",
            "completed": 125,
            "total": 500,
            "unit": "contexts",
            "detail": "searches=42",
        },
        {
            "schema_version": 1,
            "event": "scheduler_sample",
            "stage": "fix-jdf",
            "target_workers": 24,
            "active_workers": 18,
            "maximum_active_workers": 21,
            "completed_backlog": 3,
            "private_bytes": 5 * 1024**3,
            "normalized_cpu_percent": 87.5,
        },
        {
            "schema_version": 1,
            "event": "batch_completed",
            "stage": "fix-jdf",
            "batch": "a.zip",
        },
    ]
    script = (
        "import json\n"
        f"events={events!r}\n"
        "for index,event in enumerate(events):\n"
        " print('JRUTIL_PROGRESS '+json.dumps(event))\n"
        " if index == 4: print('[12:00 INF] Reading OSM stops...')\n"
    )
    reporter = _Reporter()

    result = run_command(
        [sys.executable, "-c", script],
        tmp_path,
        tmp_path / "structured.log",
        reporter,
        CommandProgress("Fix", total=2, stage="fix-jdf"),
    )

    assert result.completed == 2
    assert result.maximum_in_flight == 2
    assert result.execution_plan is not None
    assert result.execution_plan["resolved_workers"] == 10
    assert result.resource_usage == (
        {
            "schema_version": 1,
            "event": "resource_usage",
            "stage": "fix-jdf",
            "phase": "write-outputs",
            "peak_working_set_bytes": 123456,
            "spill_bytes": 654321,
        },
    )
    assert result.capture_metrics == events[6]
    assert result.scheduler_samples == (events[8],)
    assert result.maximum_workers_observed == 21
    assert reporter.completed == 2
    assert any("10 workers" in note for note in reporter.notes)
    assert any("write outputs" in detail and "last: b.zip" in detail for detail in reporter.details)
    assert any("18/24 workers" in detail and "CPU 88%" in detail for detail in reporter.details)
    assert any(
        "routing contexts: 125/500 contexts" in detail and "searches=42" in detail
        for detail in reporter.details
    )
    assert not any("Reading OSM stops" in detail for detail in reporter.details)


def test_cli_worker_overrides_and_compression_are_parsed() -> None:
    args = national_jdf._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "build",
            "--output",
            "out",
            "--jobs",
            "8",
            "--fix-jobs",
            "4",
            "--memory-budget",
            "9.5GiB",
            "--zip-compression",
            "fast",
        ]
    )

    assert args.jobs == 8
    assert args.fix_jobs == 4
    assert args.merge_jobs is None
    assert args.memory_budget == "9.5GiB"
    assert args.zip_compression == "fast"


def test_routing_cache_is_omitted_when_disabled(tmp_path: Path) -> None:
    config = BuildConfig(
        output=tmp_path / "output",
        workdir=tmp_path / "work",
        osm_file=tmp_path / "cz.osm.pbf",
        jrutil_root=None,
        jrutil_command=("jrutil",),
        geodata_root=tmp_path / "geodata",
        estimated_posts=True,
    )
    arguments = national_jdf._bundle_arguments(  # pyright: ignore[reportPrivateUsage]
        config,
        publish=tmp_path / "publish",
        logs=tmp_path / "logs",
        descriptor_path=tmp_path / "descriptor.json",
        converter_version="test",
        gvd_year=2026,
        routing_osm_file=tmp_path / "routing.osm.pbf",
        merged_zip=tmp_path / "merged.zip",
        bundle=tmp_path / "bundle",
    )
    assert not any(argument.startswith("--routing-cache=") for argument in arguments)
    cached = national_jdf._bundle_arguments(  # pyright: ignore[reportPrivateUsage]
        replace(config, routing_cache_dir=tmp_path / "cache"),
        publish=tmp_path / "publish",
        logs=tmp_path / "logs",
        descriptor_path=tmp_path / "descriptor.json",
        converter_version="test",
        gvd_year=2026,
        routing_osm_file=tmp_path / "routing.osm.pbf",
        merged_zip=tmp_path / "merged.zip",
        bundle=tmp_path / "bundle",
    )
    assert f"--routing-cache={tmp_path / 'cache'}" in cached


def test_stop_registry_arguments_reach_merge_and_bundle(tmp_path: Path) -> None:
    registry = tmp_path / "geodata" / "registry"
    config = BuildConfig(
        output=tmp_path / "output",
        workdir=tmp_path / "work",
        osm_file=tmp_path / "cz.osm.pbf",
        jrutil_root=None,
        jrutil_command=("jrutil",),
        geodata_root=tmp_path / "geodata",
        stop_registry=registry,
    )
    arguments = national_jdf._bundle_arguments(  # pyright: ignore[reportPrivateUsage]
        config,
        publish=tmp_path / "publish",
        logs=tmp_path / "logs",
        descriptor_path=tmp_path / "descriptor.json",
        converter_version="test",
        gvd_year=2026,
        routing_osm_file=None,
        merged_zip=tmp_path / "merged.zip",
        bundle=tmp_path / "bundle",
    )
    assert f"--stop-registry={registry}" in arguments
    candidates = tmp_path / "publish" / "stop-registry" / "post-candidates.csv"
    assert f"--stop-registry-candidates={candidates}" in arguments
    plain = national_jdf._bundle_arguments(  # pyright: ignore[reportPrivateUsage]
        replace(config, stop_registry=None),
        publish=tmp_path / "publish",
        logs=tmp_path / "logs",
        descriptor_path=tmp_path / "descriptor.json",
        converter_version="test",
        gvd_year=2026,
        routing_osm_file=None,
        merged_zip=tmp_path / "merged.zip",
        bundle=tmp_path / "bundle",
    )
    assert not any(argument.startswith("--stop-registry") for argument in plain)


def test_stop_registry_manifest_records_registry_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_git_identity(_repository: Path) -> dict[str, object]:
        return {"commit": "fixture"}

    monkeypatch.setattr(jrutil, "git_identity", fake_git_identity)
    registry = tmp_path / "registry"
    registry.mkdir()
    with pytest.raises(PipelineError):
        national_jdf.stop_registry_manifest(registry)
    (registry / "stops.csv").write_text("id\n", encoding="utf-8")
    (registry / "posts.csv").write_text("stop_id\n", encoding="utf-8")
    manifest = national_jdf.stop_registry_manifest(registry)
    assert [item["path"] for item in manifest["files"]] == ["stops.csv", "posts.csv"]
