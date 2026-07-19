"""Download, fix, merge, and bundle the national municipal/road JDF feeds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast
from urllib.request import Request, urlopen

from rich.console import Console
from rich.filesize import decimal
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text

VLD_URL = "https://portal.cisjr.cz/pub/JDF/JDF.zip"
DRAHY_URL = "https://portal.cisjr.cz/pub/draha/mestske/JDF.zip"
OSM_URL = "https://download.geofabrik.de/europe/czech-republic-latest.osm.pbf"
OSM_MD5_URL = OSM_URL + ".md5"
PARQUET_FILES = {
    "source_route_metadata.parquet",
    "source_stop_metadata.parquet",
    "source_call_metadata.parquet",
    "source_route_stop_zone_metadata.parquet",
    "source_notice_metadata.parquet",
    "source_transfer_metadata.parquet",
    "source_travel_restriction_metadata.parquet",
}


class PipelineError(RuntimeError):
    """A reproducible pipeline validation or execution failure."""


ProgressMode = Literal["auto", "rich", "plain", "off"]


class Reporter(Protocol):
    def stage(self, label: str) -> None: ...

    def start(self, label: str, *, total: int | None = None, unit: str = "") -> int: ...

    def update(
        self,
        task: int,
        *,
        advance: int = 0,
        completed: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None: ...

    def finish(self, task: int, detail: str = "done") -> None: ...

    def problem(self, severity: str, message: str) -> None: ...

    def note(self, message: str) -> None: ...

    def snapshot(self) -> dict[str, object]: ...

    def close(self) -> None: ...


@dataclass
class _TaskState:
    label: str
    total: int | None
    completed: int
    unit: str
    detail: str
    started: float
    last_plain_update: float
    last_plain_percent: int


class _MetricColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        if task.fields.get("unit") == "bytes":
            amount = decimal(int(task.completed))
            speed = f"{decimal(int(task.speed))}/s" if task.speed else "--/s"
            return Text(f"{amount} {speed}")
        total = f"/{int(task.total)}" if task.total is not None else ""
        return Text(f"{int(task.completed)}{total}")


class BuildReporter:
    def __init__(self, mode: ProgressMode = "auto") -> None:
        self.console = Console(stderr=True)
        if mode == "auto":
            mode = "rich" if self.console.is_terminal else "plain"
        self.mode = mode
        self.tasks: dict[int, _TaskState] = {}
        self._next_id = 1
        self._stage = "pipeline"
        self._problems: dict[tuple[str, str], int] = {}
        self._suppressed: dict[tuple[str, str], int] = {}
        self._progress: Progress | None = None
        self._rich_tasks: dict[int, TaskID] = {}
        if mode == "rich":
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                _MetricColumn(),
                TextColumn("{task.fields[detail]}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=self.console,
                transient=False,
            )
            self._progress.start()

    def stage(self, label: str) -> None:
        self._stage = label

    def start(self, label: str, *, total: int | None = None, unit: str = "") -> int:
        task_id = self._next_id
        self._next_id += 1
        now = time.monotonic()
        self.tasks[task_id] = _TaskState(label, total, 0, unit, "starting", now, now, -1)
        if self._progress is not None:
            self._rich_tasks[task_id] = self._progress.add_task(
                label, total=total, detail="starting", unit=unit
            )
        elif self.mode == "plain":
            self.note(f"START {label}")
        return task_id

    def update(
        self,
        task: int,
        *,
        advance: int = 0,
        completed: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        state = self.tasks[task]
        if total is not None:
            state.total = total
        state.completed = completed if completed is not None else state.completed + advance
        if detail is not None:
            state.detail = detail
        if self._progress is not None:
            self._progress.update(
                self._rich_tasks[task],
                completed=state.completed,
                total=state.total,
                detail=state.detail,
            )
        elif self.mode == "plain":
            now = time.monotonic()
            task_total = state.total
            percent = int(state.completed * 100 / task_total) if task_total else -1
            if now - state.last_plain_update >= 30 or percent >= state.last_plain_percent + 10:
                state.last_plain_update = now
                state.last_plain_percent = percent
                count = (
                    f"{state.completed}/{state.total}"
                    if state.total is not None
                    else str(state.completed)
                )
                transfer = ""
                if state.unit == "bytes":
                    elapsed = max(now - state.started, 0.001)
                    speed = state.completed / elapsed
                    eta = (
                        f", ETA {(state.total - state.completed) / speed:.0f}s"
                        if state.total is not None and speed > 0
                        else ""
                    )
                    transfer = f", {speed / 1_000_000:.1f} MB/s{eta}"
                self.note(
                    f"PROGRESS {state.label}: {count} {state.unit}{transfer} "
                    f"{state.detail}".rstrip()
                )

    def finish(self, task: int, detail: str = "done") -> None:
        state = self.tasks[task]
        completed = state.total if state.total is not None else state.completed
        self.update(task, completed=completed, detail=detail)
        if self._progress is not None:
            self._progress.stop_task(self._rich_tasks[task])
        elif self.mode == "plain":
            elapsed = time.monotonic() - state.started
            self.note(f"DONE {state.label} ({elapsed:.1f}s): {detail}")

    def problem(self, severity: str, message: str) -> None:
        severity = "error" if severity.lower().startswith("err") else "warning"
        key = (self._stage, severity)
        self._problems[key] = self._problems.get(key, 0) + 1
        if self._problems[key] <= 20:
            style = "bold red" if severity == "error" else "yellow"
            self.console.print(f"{severity.upper()}: {message}", style=style)
        else:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1

    def note(self, message: str) -> None:
        self.console.print(f"[{utc_now()}] {message}")

    def snapshot(self) -> dict[str, object]:
        return {
            "tasks": [asdict(state) for state in self.tasks.values()],
            "problems": {
                f"{stage}:{severity}": count for (stage, severity), count in self._problems.items()
            },
            "suppressed_problems": {
                f"{stage}:{severity}": count
                for (stage, severity), count in self._suppressed.items()
            },
        }

    def close(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
        for (stage, severity), count in self._suppressed.items():
            if count:
                self.console.print(
                    f"{count} additional {severity} messages from {stage} were retained in logs",
                    style="yellow" if severity == "warning" else "bold red",
                )


class _Response(Protocol):
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


@dataclass(frozen=True)
class DownloadRecord:
    name: str
    url: str
    retrieved_at: str
    bytes: int
    sha256: str
    etag: str | None
    last_modified: str | None
    md5: str | None = None


@dataclass(frozen=True)
class BuildConfig:
    output: Path
    repo_root: Path
    jrutil_root: Path
    geodata_root: Path
    keep_work: bool = False
    progress: ProgressMode = "auto"


DownloadFn = Callable[[str, Path, str, Reporter | None], DownloadRecord]


@dataclass(frozen=True)
class CommandProgress:
    label: str
    total: int | None = None
    event: str | None = None


@dataclass(frozen=True)
class BatchMapping:
    source: str
    original_path: str
    combined_filename: str


class CommandFailure(PipelineError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
        returncode: int,
        elapsed: float,
        tail: Sequence[str],
        last_batch: str | None,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.log_path = log_path
        self.returncode = returncode
        self.elapsed = elapsed
        self.tail = list(tail)
        self.last_batch = last_batch
        unsigned = returncode & 0xFFFFFFFF
        super().__init__(
            f"Command failed with exit code {returncode} (0x{unsigned:08X}) after "
            f"{elapsed:.1f}s; see {log_path}"
        )


CommandFn = Callable[[Sequence[str], Path, Path, Reporter | None, CommandProgress | None], None]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def file_digest(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(
    url: str,
    destination: Path,
    name: str,
    reporter: Reporter | None = None,
) -> DownloadRecord:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    retrieved_at = utc_now()
    task: int | None = None
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    downloaded = 0
    try:
        request = Request(url, headers={"User-Agent": "Obehy/0.1 national-JDF builder"})
        response_context = cast(_Response, urlopen(request, timeout=120))
        with response_context as response, temporary.open("wb") as output:
            length_text = response.headers.get("Content-Length")
            total = int(length_text) if length_text and length_text.isdigit() else None
            if reporter is not None:
                task = reporter.start(f"Download {name}", total=total, unit="bytes")
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                sha256.update(chunk)
                md5.update(chunk)
                downloaded += len(chunk)
                if reporter is not None and task is not None:
                    reporter.update(task, completed=downloaded)
            headers = response.headers
        os.replace(temporary, destination)
        if reporter is not None and task is not None:
            reporter.finish(task, f"{downloaded:,} bytes")
    except Exception:
        # Deliberately keep the .part file: failed builds retain their entire
        # staging directory for diagnosis and possible resumability work.
        raise

    return DownloadRecord(
        name=name,
        url=url,
        retrieved_at=retrieved_at,
        bytes=downloaded,
        sha256=sha256.hexdigest(),
        etag=headers.get("ETag"),
        last_modified=headers.get("Last-Modified"),
        md5=md5.hexdigest(),
    )


def _parse_md5(path: Path) -> str:
    match = re.search(r"(?i)\b([0-9a-f]{32})\b", path.read_text(encoding="ascii"))
    if match is None:
        raise PipelineError("Geofabrik MD5 sidecar did not contain an MD5 digest")
    return match.group(1).lower()


def download_osm(
    destination: Path,
    sidecar: Path,
    download: DownloadFn = download_file,
    reporter: Reporter | None = None,
) -> DownloadRecord:
    last_error = ""
    for _attempt in range(2):
        download(OSM_MD5_URL, sidecar, "osm-md5", reporter)
        expected_md5 = _parse_md5(sidecar)
        record = download(OSM_URL, destination, "osm", reporter)
        actual_md5 = record.md5 or file_digest(destination, "md5")
        if actual_md5 == expected_md5:
            sidecar.unlink(missing_ok=True)
            return replace(record, md5=actual_md5)
        last_error = f"expected {expected_md5}, got {actual_md5}"
    raise PipelineError(f"Geofabrik OSM checksum mismatch after retry: {last_error}")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


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


def extract_zip_safely(
    archive_path: Path,
    destination: Path,
    reporter: Reporter | None = None,
    label: str | None = None,
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        entries = _validated_zip_entries(archive)
        task = (
            reporter.start(label or f"Extract {archive_path.name}", total=len(entries))
            if reporter
            else None
        )
        for info in entries:
            target = destination.joinpath(*PurePosixPath(info.filename).parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            if reporter is not None and task is not None:
                reporter.update(task, advance=1)
        if reporter is not None and task is not None:
            reporter.finish(task, f"{len(entries)} entries")


def discover_jdf_batches(
    extracted_root: Path,
    reporter: Reporter | None = None,
    label: str | None = None,
) -> list[Path]:
    batches = sorted(
        (
            path
            for path in extracted_root.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".zip"
        ),
        key=lambda path: path.as_posix(),
    )
    names: dict[str, Path] = {}
    task = (
        reporter.start(label or "Validate nested JDF ZIPs", total=len(batches))
        if reporter
        else None
    )
    for batch in batches:
        key = batch.stem.casefold()
        if previous := names.get(key):
            raise PipelineError(f"Duplicate JDF batch basename: {previous} and {batch}")
        names[key] = batch
        try:
            with zipfile.ZipFile(batch) as archive:
                entries = _validated_zip_entries(archive)
        except zipfile.BadZipFile as error:
            raise PipelineError(f"Malformed nested JDF ZIP: {batch}") from error
        versions = [
            info
            for info in entries
            if PurePosixPath(info.filename).name.casefold() == "verzejdf.txt"
        ]
        if len(versions) != 1:
            raise PipelineError(f"JDF ZIP must contain exactly one VerzeJDF.txt: {batch}")
        if reporter is not None and task is not None:
            reporter.update(task, advance=1)
    if not batches:
        raise PipelineError(f"Downloaded archive contains no nested JDF batches: {extracted_root}")
    if reporter is not None and task is not None:
        reporter.finish(task, f"{len(batches)} batches")
    return batches


def combine_batches(
    sources: Sequence[tuple[str, Path, Sequence[Path]]],
    destination: Path,
    reporter: Reporter | None = None,
) -> list[BatchMapping]:
    destination.mkdir(parents=True, exist_ok=False)
    total = sum(len(batches) for _, _, batches in sources)
    task = reporter.start("Combine national batches", total=total) if reporter else None
    mappings: list[BatchMapping] = []
    names: set[str] = set()
    for source_name, source_root, batches in sources:
        for batch in batches:
            combined_name = f"{source_name}-{batch.stem}.zip"
            key = combined_name.casefold()
            if key in names:
                raise PipelineError(
                    f"Case-insensitive combined JDF batch collision: {combined_name}"
                )
            names.add(key)
            shutil.move(str(batch), destination / combined_name)
            mappings.append(
                BatchMapping(
                    source=source_name,
                    original_path=batch.relative_to(source_root).as_posix(),
                    combined_filename=combined_name,
                )
            )
            if reporter is not None and task is not None:
                reporter.update(task, advance=1)
    if reporter is not None and task is not None:
        reporter.finish(task, f"{len(mappings)} batches")
    return mappings


def deterministic_zip(
    source_directory: Path,
    destination: Path,
    reporter: Reporter | None = None,
) -> None:
    files = sorted(
        (path for path in source_directory.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(source_directory).as_posix(),
    )
    if not files:
        raise PipelineError(f"Cannot package empty directory: {source_directory}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    task = reporter.start("Package merged JDF", total=len(files)) if reporter else None
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in files:
            relative = path.relative_to(source_directory).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )
            if reporter is not None and task is not None:
                reporter.update(task, advance=1)
    if reporter is not None and task is not None:
        reporter.finish(task, f"{len(files)} files")


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOG_SEVERITY = re.compile(r"\[(?:[^\]]*\s)?(?P<severity>WRN|ERR)\]")


def run_command(
    command: Sequence[str],
    cwd: Path,
    log_path: Path,
    reporter: Reporter | None = None,
    progress: CommandProgress | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if reporter is not None and progress is not None:
        reporter.stage(progress.label)
    task = reporter.start(progress.label, total=progress.total) if reporter and progress else None
    tail: deque[str] = deque(maxlen=60)
    last_batch: str | None = None
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert process.stdout is not None
    with log_path.open("wb") as log:
        for raw_line in process.stdout:
            log.write(raw_line)
            log.flush()
            clean = _ANSI_ESCAPE.sub("", raw_line.decode("utf-8", errors="replace")).rstrip("\r\n")
            tail.append(clean)
            if progress and progress.event and progress.event in clean:
                last_batch = clean.split(progress.event, 1)[1].strip(" :") or clean
                if reporter is not None and task is not None:
                    reporter.update(task, advance=1, detail=last_batch)
            elif reporter is not None and task is not None:
                phase = next(
                    (
                        marker
                        for marker in (
                            "Reading external stops",
                            "Reading OSM stops",
                            "Creating stop matcher",
                            "Creating Czech town name matcher",
                            "Creating European town name matcher",
                            "Resolving route overlaps",
                            "Writing merged JDF",
                            "Bundle phase:",
                        )
                        if marker in clean
                    ),
                    None,
                )
                if phase is not None:
                    reporter.update(task, detail=clean)
            severity = _LOG_SEVERITY.search(clean)
            if reporter is not None and severity is not None:
                reporter.problem(severity.group("severity"), clean)
    returncode = process.wait()
    elapsed = time.monotonic() - started
    if returncode != 0:
        raise CommandFailure(
            command=command,
            cwd=cwd,
            log_path=log_path,
            returncode=returncode,
            elapsed=elapsed,
            tail=tail,
            last_batch=last_batch,
        )
    if reporter is not None and task is not None:
        reporter.finish(task, f"completed in {elapsed:.1f}s")


def _git_identity(repository: Path) -> dict[str, Any]:
    safe = f"safe.directory={repository.resolve().as_posix()}"
    commit = subprocess.run(
        ["git", "-c", safe, "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-c", safe, "-C", str(repository), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    identity: dict[str, Any] = {
        "commit": commit,
        "dirty": bool(status),
        "status": status.splitlines(),
    }
    if status:
        diff = subprocess.run(
            ["git", "-c", safe, "-C", str(repository), "diff", "--binary", "HEAD"],
            capture_output=True,
            check=True,
        ).stdout
        identity["working_tree_sha256"] = hashlib.sha256(diff).hexdigest()
    return identity


def geodata_manifest(geodata_directory: Path) -> dict[str, Any]:
    files = sorted(geodata_directory.rglob("*.csv"), key=lambda path: path.as_posix())
    if not files:
        raise PipelineError(f"Geodata directory contains no CSV files: {geodata_directory}")
    return {
        "repository": _git_identity(geodata_directory.parent),
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
    project = config.jrutil_root / "jrutil-multitool" / "jrutil-multitool.fsproj"
    if not project.is_file():
        raise PipelineError(f"Root-level JrUtil multitool project not found: {project}")
    return ["dotnet", "run", "--project", str(project), "--no-restore", "--", *arguments]


def _verify_fixed_batches(fixed_root: Path, expected: set[str]) -> None:
    versions = list(fixed_root.rglob("VerzeJDF.txt"))
    actual = [path.parent.relative_to(fixed_root).as_posix() for path in versions]
    duplicates = sorted(name for name in set(actual) if actual.count(name) > 1)
    actual_set = set(actual)
    if duplicates or actual_set != expected:
        raise PipelineError(
            f"Fixed batch accounting mismatch for {fixed_root}: "
            f"missing={sorted(expected - actual_set)}, "
            f"unexpected={sorted(actual_set - expected)}, duplicates={duplicates}"
        )


def _verify_bundle(bundle: Path, reporter: Reporter | None = None) -> dict[str, Any]:
    manifest_path = bundle / "manifest.json"
    diagnostics_path = bundle / "diagnostics.json"
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    declared_paths: set[str] = set()
    entries = cast(list[dict[str, Any]], manifest.get("files", []))
    task = reporter.start("Validate bundle", total=len(entries)) if reporter else None
    for raw_entry in entries:
        relative = cast(str, raw_entry["path"])
        declared_paths.add(relative)
        payload = bundle / Path(relative)
        if not payload.is_file():
            raise PipelineError(f"Bundle manifest payload is missing: {relative}")
        if (
            payload.stat().st_size != raw_entry["bytes"]
            or file_digest(payload) != raw_entry["sha256"]
        ):
            raise PipelineError(f"Bundle manifest does not match payload: {relative}")
        if reporter is not None and task is not None:
            reporter.update(task, advance=1, detail=relative)
    missing_parquet = PARQUET_FILES - declared_paths
    if missing_parquet:
        raise PipelineError(f"Bundle is missing required Parquet files: {sorted(missing_parquet)}")
    diagnostics = cast(dict[str, Any], json.loads(diagnostics_path.read_text(encoding="utf-8")))
    errors = [
        item
        for item in cast(list[dict[str, Any]], diagnostics["diagnostics"])
        if item["severity"] == "error"
    ]
    if errors:
        raise PipelineError(f"Bundle contains {len(errors)} error-severity diagnostics")
    trips = bundle / "gtfs-intermediate" / "trips.txt"
    if not trips.is_file() or len(trips.read_text(encoding="utf-8-sig").splitlines()) < 2:
        raise PipelineError("Bundle GTFS contains no trips")
    if reporter is not None and task is not None:
        reporter.finish(task, f"{len(entries)} payloads")
    return manifest


def _converter_version(identity: Mapping[str, Any]) -> str:
    commit = cast(str, identity["commit"])
    dirty_hash = identity.get("working_tree_sha256")
    return commit if dirty_hash is None else f"{commit}+dirty.{cast(str, dirty_hash)[:12]}"


def build(
    config: BuildConfig,
    download: DownloadFn = download_file,
    command_runner: CommandFn = run_command,
    reporter: Reporter | None = None,
) -> Path:
    output = config.output.resolve()
    if output.exists():
        raise PipelineError(f"Output path must not exist: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.parent / f".{output.name}.work-{uuid.uuid4().hex}"
    stage.mkdir()
    sources = stage / "sources"
    derived = stage / "derived"
    bundle = stage / "bundle"
    logs = stage / "logs"
    work = stage / "work"
    for directory in (sources, derived, logs, work):
        directory.mkdir(parents=True)

    active_stage = "initialization"
    owned_reporter = reporter is None
    reporter = reporter or BuildReporter(config.progress)
    try:
        active_stage = "download-vld"
        vld = download(VLD_URL, sources / "JDF_VLD.zip", "VLD", reporter)
        active_stage = "download-drahy"
        drahy = download(DRAHY_URL, sources / "JDF_drahy.zip", "dráhy", reporter)
        active_stage = "download-osm"
        osm = download_osm(
            sources / "czech-republic.osm.pbf",
            sources / "czech-republic-latest.osm.pbf.md5",
            download,
            reporter,
        )
        write_json(
            sources / "sources.json",
            {"schema_version": 1, "sources": [asdict(vld), asdict(drahy), asdict(osm)]},
        )

        extracted_vld = work / "extracted" / "vld"
        extracted_drahy = work / "extracted" / "drahy"
        active_stage = "extract-vld"
        extract_zip_safely(sources / "JDF_VLD.zip", extracted_vld, reporter, "Extract VLD")
        active_stage = "extract-drahy"
        extract_zip_safely(sources / "JDF_drahy.zip", extracted_drahy, reporter, "Extract dráhy")
        active_stage = "validate-vld"
        vld_batches = discover_jdf_batches(extracted_vld, reporter, "Validate VLD batches")
        active_stage = "validate-drahy"
        drahy_batches = discover_jdf_batches(extracted_drahy, reporter, "Validate dráhy batches")

        active_stage = "combine-batches"
        combined_root = work / "batches"
        mappings = combine_batches(
            (
                ("vld", extracted_vld, vld_batches),
                ("drahy", extracted_drahy, drahy_batches),
            ),
            combined_root,
            reporter,
        )

        active_stage = "provenance"
        jrutil_identity = _git_identity(config.jrutil_root)
        geodata = geodata_manifest(config.geodata_root)
        fixed_root = work / "fixed"

        active_stage = "fix-national-jdf"
        command_runner(
            _multitool_command(
                config,
                [
                    "fix-jdf",
                    "--strict",
                    f"--ext-geodata={config.geodata_root}",
                    f"--cz-pbf={sources / 'czech-republic.osm.pbf'}",
                    f"--logfile={logs / 'fix.log'}",
                    str(combined_root),
                    str(fixed_root),
                ],
            ),
            config.jrutil_root,
            logs / "fix.process.log",
            reporter,
            CommandProgress(
                "Fix national JDF",
                total=len(mappings),
                event="Processing JDF batch",
            ),
        )
        expected_fixed = {Path(item.combined_filename).stem for item in mappings}
        _verify_fixed_batches(fixed_root, expected_fixed)

        active_stage = "merge-national-jdf"
        merged_directory = work / "merged-jdf"
        command_runner(
            _multitool_command(
                config,
                [
                    "merge-jdf",
                    "--strict",
                    f"--logfile={logs / 'merge.log'}",
                    str(merged_directory),
                    str(fixed_root),
                ],
            ),
            config.jrutil_root,
            logs / "merge.process.log",
            reporter,
            CommandProgress(
                "Merge national JDF",
                total=len(mappings),
                event="Merging JDF batch",
            ),
        )
        active_stage = "package-merged-jdf"
        merged_zip = derived / "merged-jdf.zip"
        deterministic_zip(merged_directory, merged_zip, reporter)
        descriptor = {
            "schema_version": 1,
            "source_id": "national-jdf-vld-drahy",
            "retrieved_at": max(vld.retrieved_at, drahy.retrieved_at, osm.retrieved_at),
            "retrieval_method": "derived-from-https-geofabrik-and-pinned-geodata",
            "source_uri": "obehy:derived:national-jdf-vld-drahy",
            "licence": "CIS JŘ public data; OSM ODbL; external geodata source-specific",
            "payload_kind": "zip",
            "payload_sha256": file_digest(merged_zip),
            "payload_bytes": merged_zip.stat().st_size,
        }
        descriptor_path = derived / "snapshot-descriptor.json"
        write_json(descriptor_path, descriptor)

        active_stage = "generate-bundle"
        command_runner(
            _multitool_command(
                config,
                [
                    "jdf-to-bundle",
                    f"--snapshot-descriptor={descriptor_path}",
                    f"--converter-version={_converter_version(jrutil_identity)}",
                    f"--logfile={logs / 'bundle.log'}",
                    str(merged_zip),
                    str(bundle),
                ],
            ),
            config.jrutil_root,
            logs / "bundle.process.log",
            reporter,
            CommandProgress("Generate GTFS + Parquet bundle"),
        )
        active_stage = "validate-bundle"
        bundle_manifest = _verify_bundle(bundle, reporter)
        run_manifest = {
            "schema_version": 1,
            "completed_at": utc_now(),
            "sources_manifest_sha256": file_digest(sources / "sources.json"),
            "geodata": geodata,
            "jrutil": jrutil_identity,
            "conversion": {"stop_ids_cis": False, "stop_merge": "name", "strict": True},
            "batch_counts": {
                "vld": len(vld_batches),
                "drahy": len(drahy_batches),
                "total": len(vld_batches) + len(drahy_batches),
            },
            "batch_mapping": [asdict(mapping) for mapping in mappings],
            "merged_jdf": {"bytes": merged_zip.stat().st_size, "sha256": file_digest(merged_zip)},
            "bundle_manifest_sha256": file_digest(bundle / "manifest.json"),
            "bundle_file_count": len(cast(list[object], bundle_manifest["files"])),
        }
        write_json(stage / "run-manifest.json", run_manifest)
        if not config.keep_work:
            shutil.rmtree(work)
        os.replace(stage, output)
        return output
    except Exception as error:
        failure: dict[str, Any] = {
            "schema_version": 1,
            "failed_at": utc_now(),
            "stage": active_stage,
            "error_type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
            "progress": reporter.snapshot(),
            "staging_directory": str(stage),
            "logs_directory": str(logs),
        }
        if isinstance(error, CommandFailure):
            failure["command"] = error.command
            failure["working_directory"] = str(error.cwd)
            failure["exit_code"] = error.returncode
            failure["exit_code_hex"] = f"0x{error.returncode & 0xFFFFFFFF:08X}"
            failure["elapsed_seconds"] = error.elapsed
            failure["last_batch"] = error.last_batch
            failure["process_log"] = str(error.log_path)
            failure["process_output_tail"] = error.tail
        failure_path = logs / "failure.json"
        write_json(failure_path, failure)
        reporter.problem("error", f"Stage {active_stage} failed: {error}")
        if isinstance(error, CommandFailure):
            reporter.note(
                f"Last batch: {error.last_batch or 'none reported'}; process log: {error.log_path}"
            )
            if error.tail:
                reporter.note("Last process output:\n" + "\n".join(error.tail[-12:]))
        reporter.note(f"FAILED STAGING RETAINED: {stage}")
        reporter.note(f"Failure report: {failure_path}")
        raise
    finally:
        if owned_reporter:
            reporter.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obehy-national-jdf")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build a national JDF conversion bundle")
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--jrutil-root", type=Path)
    build_parser.add_argument("--geodata-root", type=Path)
    build_parser.add_argument("--keep-work", action="store_true")
    build_parser.add_argument(
        "--progress",
        choices=("auto", "rich", "plain", "off"),
        default="auto",
        help="terminal progress mode (default: auto)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    output = cast(Path, args.output)
    config = BuildConfig(
        output=output,
        repo_root=repo_root,
        jrutil_root=cast(Path | None, args.jrutil_root) or repo_root.parent / "jrutil",
        geodata_root=cast(Path | None, args.geodata_root)
        or repo_root.parent / "jrunify-ext-geodata" / "other",
        keep_work=cast(bool, args.keep_work),
        progress=cast(ProgressMode, args.progress),
    )
    try:
        result = build(config)
    except (OSError, PipelineError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"National JDF bundle written to {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
