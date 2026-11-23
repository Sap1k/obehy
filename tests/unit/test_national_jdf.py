from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from obehy import national_jdf
from obehy.pipeline import download, reporting
from obehy.pipeline.download import DownloadRecord, download_file
from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import file_digest
from obehy.pipeline.process import CommandFailure, run_command
from obehy.pipeline.reporting import BuildReporter, CommandProgress


def _no_sleep(_seconds: float) -> None:
    return


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

    with pytest.raises(download.DownloadError, match=r"data failed after 1 attempt: .*lost"):
        download_file("https://example.invalid/data", tmp_path / "data", "data")

    assert (tmp_path / "data.part").read_bytes() == b"partial"


def test_download_retries_transient_failures_and_logs_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    delays: list[float] = []

    def flaky_urlopen(*_args: object, **_kwargs: object) -> _Response:
        calls.append(1)
        if len(calls) < 3:
            raise URLError(TimeoutError("_ssl.c:1015: The handshake operation timed out"))
        return _Response([b"payload"], 7)

    monkeypatch.setattr(download, "urlopen", flaky_urlopen)
    monkeypatch.setattr(download, "_sleep", delays.append)
    reporter = _Reporter()

    with download.fetch_log() as log:
        record = download_file("https://example.invalid/data", tmp_path / "data", "data", reporter)
    attempts = log.attempts

    assert record.bytes == 7
    assert delays == list(download.RETRY_DELAYS)
    assert [attempt["outcome"] for attempt in attempts] == ["retry", "retry", "ok"]
    assert "handshake operation timed out" in str(attempts[0]["error"])
    assert any("retrying in 5s" in note for note in reporter.notes)


def test_download_gives_up_with_a_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout_urlopen(*_args: object, **_kwargs: object) -> _Response:
        raise URLError(TimeoutError("handshake timed out"))

    monkeypatch.setattr(download, "urlopen", timeout_urlopen)
    monkeypatch.setattr(download, "_sleep", _no_sleep)

    with pytest.raises(
        download.DownloadError,
        match=r"Download PID failed after 3 attempts: https://example.invalid/pid.zip: handshake",
    ):
        download_file("https://example.invalid/pid.zip", tmp_path / "pid.zip", "PID")


def test_download_does_not_retry_client_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def missing_urlopen(url: object, *_args: object, **_kwargs: object) -> _Response:
        calls.append(1)
        raise HTTPError("https://example.invalid/data", 404, "Not Found", Message(), None)

    monkeypatch.setattr(download, "urlopen", missing_urlopen)

    with pytest.raises(download.DownloadError, match=r"after 1 attempt: .*HTTP 404 Not Found"):
        download_file("https://example.invalid/data", tmp_path / "data", "data")
    assert len(calls) == 1


def test_download_sources_records_both_exports(tmp_path: Path) -> None:
    def fake_download(
        url: str, destination: Path, name: str, _reporter: object = None
    ) -> DownloadRecord:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(name.encode("utf-8"))
        return _download_record(name, url, destination)

    vld, drahy = national_jdf.download_sources(fake_download, tmp_path, None)

    assert (tmp_path / "JDF_drahy.zip").read_bytes() == "dráhy".encode()
    manifest = json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))
    assert manifest["sources"] == [asdict(vld), asdict(drahy)]


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


def test_run_command_adds_environment_variables(tmp_path: Path) -> None:
    script = "import os;print(os.environ['RAYON_NUM_THREADS'], os.environ.get('PATH') is not None)"
    log = tmp_path / "env.log"

    run_command([sys.executable, "-c", script], tmp_path, log, env={"RAYON_NUM_THREADS": "3"})

    assert log.read_text(encoding="utf-8").split() == ["3", "True"]
