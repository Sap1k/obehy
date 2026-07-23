from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import pytest

from obehy import national_jdf
from obehy.national_jdf import (
    BuildConfig,
    CommandFailure,
    CommandProgress,
    DownloadRecord,
    PipelineError,
    build,
    combine_batches,
    deterministic_zip,
    discover_jdf_batches,
    download_file,
    download_osm,
    extract_zip_safely,
    file_digest,
    run_command,
    stage_nested_jdf_batches,
)

WORKSPACE = Path(__file__).parents[3]


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


def test_safe_extract_rejects_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    archive.write_bytes(_zip_bytes({"../outside.txt": b"bad"}))

    with pytest.raises(PipelineError, match="Unsafe ZIP entry"):
        extract_zip_safely(archive, tmp_path / "output")

    assert not (tmp_path / "outside.txt").exists()


def test_discover_batches_rejects_duplicate_line_local_archive_names(tmp_path: Path) -> None:
    payload = _zip_bytes({"VerzeJDF.txt": b'"1.11";\r\n'})
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "1.zip").write_bytes(payload)
    (tmp_path / "b" / "1.ZIP").write_bytes(payload)

    with pytest.raises(PipelineError, match="Duplicate JDF batch basename"):
        discover_jdf_batches(tmp_path)


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


def test_osm_checksum_rollover_retries_once(tmp_path: Path) -> None:
    pbf = tmp_path / "czech.osm.pbf"
    sidecar = tmp_path / "czech.osm.pbf.md5"
    calls: list[str] = []
    expected_payload = b"second-version"

    def fake_download(
        url: str, destination: Path, name: str, _reporter: object = None
    ) -> DownloadRecord:
        calls.append(name)
        if name == "osm-md5":
            destination.write_text(
                hashlib.md5(expected_payload).hexdigest() + "  czech.osm.pbf\n",
                encoding="ascii",
            )
        else:
            destination.write_bytes(
                b"first-version" if calls.count("osm") == 1 else expected_payload
            )
        return _download_record(name, url, destination)

    record = download_osm(pbf, sidecar, fake_download)

    assert calls == ["osm-md5", "osm", "osm-md5", "osm"]
    assert record.md5 == hashlib.md5(expected_payload).hexdigest()
    assert not sidecar.exists()


@pytest.mark.parametrize("keep_work", [False, True])
def test_build_orchestrates_fix_merge_and_bundle_atomically(
    tmp_path: Path, keep_work: bool, monkeypatch: pytest.MonkeyPatch
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

    monkeypatch.setattr(national_jdf, "_git_identity", fake_git_identity)
    commands: list[list[str]] = []
    osm_payload = b"fixture-osm"

    def fake_download(
        url: str, destination: Path, name: str, _reporter: object = None
    ) -> DownloadRecord:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if name == "osm-md5":
            destination.write_text(
                hashlib.md5(osm_payload).hexdigest() + "  czech-republic-latest.osm.pbf\n",
                encoding="ascii",
            )
        elif name == "osm":
            destination.write_bytes(osm_payload)
        else:
            inner = _zip_bytes({"VerzeJDF.txt": b'"1.11";\r\n'})
            destination.write_bytes(_zip_bytes({"1.zip": inner}))
        return _download_record(name, url, destination)

    def fake_command(
        command: Sequence[str],
        _cwd: Path,
        log: Path,
        _reporter: object = None,
        _progress: object = None,
    ) -> None:
        command = list(command)
        commands.append(command)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fixture command\n", encoding="utf-8")
        if command[1] == "build":
            return
        arguments = command[command.index("--") + 1 :]
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
        elif operation == "jdf-to-bundle":
            bundle = Path(arguments[-1])
            (bundle / "gtfs-intermediate").mkdir(parents=True)
            (bundle / "gtfs-intermediate" / "trips.txt").write_text(
                "route_id,service_id,trip_id\nr,s,t\n", encoding="utf-8"
            )
            (bundle / "gtfs-intermediate" / "stops.txt").write_text(
                "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                "s,Stop,50,14,0,p\n"
                "p,Station,50,14,1,\n",
                encoding="utf-8",
            )
            (bundle / "gtfs-intermediate" / "stop_times.txt").write_text(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "t,08:00:00,08:00:00,s,1\n",
                encoding="utf-8",
            )
            for name in national_jdf.PARQUET_FILES:
                (bundle / name).write_bytes(b"PAR1")
            diagnostics: dict[str, object] = {"schema_version": 1, "diagnostics": []}
            national_jdf.write_json(bundle / "diagnostics.json", diagnostics)
            payloads = [bundle / "diagnostics.json"]
            payloads.extend(
                (bundle / "gtfs-intermediate" / name)
                for name in ("trips.txt", "stops.txt", "stop_times.txt")
            )
            payloads.extend(bundle / name for name in national_jdf.PARQUET_FILES)
            manifest = {
                "files": [
                    {
                        "path": path.relative_to(bundle).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": file_digest(path),
                    }
                    for path in payloads
                ]
            }
            national_jdf.write_json(bundle / "manifest.json", manifest)
        else:
            raise AssertionError(f"Unexpected operation: {operation}")

    result = build(
        BuildConfig(
            output=output,
            repo_root=tmp_path / "repo",
            jrutil_root=jrutil_root,
            geodata_root=geodata_root,
            progress="off",
            keep_work=keep_work,
        ),
        fake_download,
        fake_command,
    )

    assert result == output
    assert (output / "derived" / "merged-jdf.zip").is_file()
    assert (output / "bundle" / "manifest.json").is_file()
    assert (output / "work").exists() is keep_work
    if keep_work:
        assert len(list((output / "work" / "fixed").glob("*.zip"))) == 2
    assert commands[0][1] == "build"
    multitool_commands = [command for command in commands if "--" in command]
    operations = [command[command.index("--") + 1] for command in multitool_commands]
    assert operations == ["fix-jdf", "merge-jdf", "jdf-to-bundle"]
    assert all("--strict" in command for command in multitool_commands[:2])
    assert all("--by-id" not in command for command in multitool_commands)
    assert all("--stop-ids-cis" not in command for command in multitool_commands)
    fix_command, merge_command, bundle_command = multitool_commands
    assert "--batch-output=zip" in fix_command
    assert "--jobs=auto" in fix_command
    assert "--jobs=auto" in merge_command
    assert "--memory-budget=auto" in fix_command
    assert "--memory-budget=auto" in merge_command
    assert any(argument.startswith("--ext-geodata=") for argument in fix_command)
    assert any(argument.startswith("--cz-pbf=") for argument in fix_command)
    assert not any(argument.startswith("--ext-geodata=") for argument in merge_command)
    assert not any(argument.startswith("--cz-pbf=") for argument in merge_command)
    assert "--international-route-policy=regional-adjacent" in fix_command
    assert "--international-route-policy=regional-adjacent" in bundle_command
    assert all(
        not any(argument.startswith("--cache=") for argument in command)
        for command in multitool_commands
    )
    run_manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["batch_counts"] == {"drahy": 1, "total": 2, "vld": 1}
    assert run_manifest["batch_mapping"] == [
        {"combined_filename": "vld-1.zip", "original_path": "1.zip", "source": "vld"},
        {
            "combined_filename": "drahy-1.zip",
            "original_path": "1.zip",
            "source": "drahy",
        },
    ]
    assert run_manifest["conversion"] == {
        "international_route_policy": "regional-adjacent",
        "stop_ids_cis": False,
        "stop_merge": "name",
        "strict": True,
    }
    assert run_manifest["execution"]["requested"] == {
        "fix_jobs": "auto",
        "jobs": "auto",
        "memory_budget": "auto",
        "merge_jobs": "auto",
    }
    assert run_manifest["merged_jdf"]["compression"] == "balanced"
    assert run_manifest["merged_jdf"]["compression_level"] == 6
    assert run_manifest["jrutil"] == {
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "dirty": False,
        "status": [],
    }
    assert [file["path"] for file in run_manifest["geodata"]["files"]] == ["fixture.csv"]


def test_gtfs_stop_verifier_allows_zero_coordinates_with_aggregate_warning(
    tmp_path: Path,
) -> None:
    (tmp_path / "stops.txt").write_text(
        "stop_id,stop_lat,stop_lon,location_type,parent_station\ns,0,0,0,p\np,50,14,1,\n",
        encoding="utf-8",
    )
    (tmp_path / "stop_times.txt").write_text("trip_id,stop_id\nt,s\n", encoding="utf-8")
    reporter = national_jdf.BuildReporter("off")

    national_jdf.verify_gtfs_stops(tmp_path, reporter)

    assert reporter.snapshot()["problems"] == {"pipeline:warning": 1}


def test_gtfs_stop_verifier_rejects_unreferenced_boarding_stop(tmp_path: Path) -> None:
    (tmp_path / "stops.txt").write_text(
        "stop_id,stop_lat,stop_lon,location_type,parent_station\nused,50,14,0,\norphan,50,14,0,\n",
        encoding="utf-8",
    )
    (tmp_path / "stop_times.txt").write_text("trip_id,stop_id\nt,used\n", encoding="utf-8")

    with pytest.raises(national_jdf.PipelineError, match="unreferenced"):
        national_jdf.verify_gtfs_stops(tmp_path)


def test_build_retains_staging_directory_after_failure(tmp_path: Path) -> None:
    output = tmp_path / "failed-output"

    def failing_download(
        _url: str, _destination: Path, _name: str, _reporter: object = None
    ) -> DownloadRecord:
        raise OSError("fixture download failure")

    with pytest.raises(OSError, match="fixture download failure"):
        build(
            BuildConfig(
                output=output,
                repo_root=WORKSPACE / "repo",
                jrutil_root=WORKSPACE / "jrutil",
                geodata_root=WORKSPACE / "jrunify-ext-geodata" / "other",
                progress="off",
            ),
            failing_download,
        )

    assert not output.exists()
    retained = list(tmp_path.glob(".failed-output.work-*"))
    assert len(retained) == 1
    assert (retained[0] / "publish" / "sources").is_dir()
    failure = json.loads((retained[0] / "publish" / "logs" / "failure.json").read_text())
    assert failure["stage"] == "download-vld"
    assert failure["message"] == "fixture download failure"


def test_combine_batches_prefixes_identical_names_and_moves_files(tmp_path: Path) -> None:
    vld = tmp_path / "vld"
    drahy = tmp_path / "drahy"
    vld.mkdir()
    drahy.mkdir()
    (vld / "1.zip").write_bytes(b"vld")
    (drahy / "1.zip").write_bytes(b"drahy")

    mappings = combine_batches(
        (("vld", vld, [vld / "1.zip"]), ("drahy", drahy, [drahy / "1.zip"])),
        tmp_path / "combined",
    )

    assert [item.combined_filename for item in mappings] == ["vld-1.zip", "drahy-1.zip"]
    assert (tmp_path / "combined" / "vld-1.zip").read_bytes() == b"vld"
    assert (tmp_path / "combined" / "drahy-1.zip").read_bytes() == b"drahy"
    assert not (vld / "1.zip").exists()
    assert not (drahy / "1.zip").exists()


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


def test_rich_indeterminate_task_gets_a_finished_lifecycle_state() -> None:
    reporter = national_jdf.BuildReporter("rich")
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
        status_column = national_jdf._LifecycleSpinnerColumn()  # pyright: ignore[reportPrivateUsage]
        rendered_status = status_column.render(rich_task)
        assert str(rendered_status) == "✓"
        bar_column = national_jdf._LifecycleBarColumn()  # pyright: ignore[reportPrivateUsage]
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

    monkeypatch.setattr(national_jdf, "urlopen", fake_urlopen)
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

    monkeypatch.setattr(national_jdf, "urlopen", interrupted_urlopen)

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
    assert reporter.completed == 2
    assert any("10 workers" in note for note in reporter.notes)
    assert any("write outputs" in detail and "last: b.zip" in detail for detail in reporter.details)
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
