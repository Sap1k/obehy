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


def test_build_orchestrates_fix_merge_and_bundle_atomically(tmp_path: Path) -> None:
    output = tmp_path / "national"
    jrutil_root = WORKSPACE / "jrutil"
    geodata_root = WORKSPACE / "jrunify-ext-geodata" / "other"
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
        arguments = command[command.index("--") + 1 :]
        operation = arguments[0]
        if operation == "fix-jdf":
            input_root = Path(arguments[-2])
            output_root = Path(arguments[-1])
            for archive in input_root.rglob("*.zip"):
                batch = output_root / archive.stem
                batch.mkdir(parents=True)
                (batch / "VerzeJDF.txt").write_text('"1.11";\r\n', encoding="cp1250")
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
            for name in national_jdf.PARQUET_FILES:
                (bundle / name).write_bytes(b"PAR1")
            diagnostics: dict[str, object] = {"schema_version": 1, "diagnostics": []}
            national_jdf.write_json(bundle / "diagnostics.json", diagnostics)
            payloads = [bundle / "diagnostics.json", bundle / "gtfs-intermediate" / "trips.txt"]
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
            repo_root=WORKSPACE / "repo",
            jrutil_root=jrutil_root,
            geodata_root=geodata_root,
            progress="off",
        ),
        fake_download,
        fake_command,
    )

    assert result == output
    assert (output / "derived" / "merged-jdf.zip").is_file()
    assert (output / "bundle" / "manifest.json").is_file()
    assert not (output / "work").exists()
    operations = [command[command.index("--") + 1] for command in commands]
    assert operations == ["fix-jdf", "merge-jdf", "jdf-to-bundle"]
    assert all("--strict" in command for command in commands[:2])
    assert all("--by-id" not in command for command in commands)
    assert all("--stop-ids-cis" not in command for command in commands)
    assert all(
        not any(argument.startswith("--cache=") for argument in command) for command in commands
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
        "stop_ids_cis": False,
        "stop_merge": "name",
        "strict": True,
    }


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
    assert (retained[0] / "sources").is_dir()
    failure = json.loads((retained[0] / "logs" / "failure.json").read_text())
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
        self.problems: list[tuple[str, str]] = []

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
        del task, total, detail
        self.completed = completed if completed is not None else self.completed + advance

    def finish(self, task: int, detail: str = "done") -> None:
        del task, detail

    def problem(self, severity: str, message: str) -> None:
        self.problems.append((severity, message))

    def note(self, message: str) -> None:
        del message

    def snapshot(self) -> dict[str, object]:
        return {}

    def close(self) -> None: ...


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
    monkeypatch.setattr(national_jdf, "urlopen", lambda *_args, **_kwargs: response)
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

    monkeypatch.setattr(
        national_jdf, "urlopen", lambda *_args, **_kwargs: Interrupted([b"partial"])
    )

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
