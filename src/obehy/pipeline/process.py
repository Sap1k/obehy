"""Supervised subprocess execution with JrUtil progress parsing."""

from __future__ import annotations

import json
import re
import subprocess
import time
import traceback
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import utc_now
from obehy.pipeline.reporting import CommandProgress, Reporter, StageClock


@dataclass(frozen=True)
class CommandResult:
    elapsed_seconds: float
    completed: int
    total: int | None
    execution_plan: dict[str, object] | None
    failed_batch: str | None
    maximum_in_flight: int
    resource_usage: tuple[dict[str, object], ...]
    capture_metrics: dict[str, object] | None = None
    scheduler_samples: tuple[dict[str, object], ...] = ()
    maximum_workers_observed: int = 0


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
        completed: int = 0,
        in_flight: Sequence[str] = (),
        execution_plan: Mapping[str, object] | None = None,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.log_path = log_path
        self.returncode = returncode
        self.elapsed = elapsed
        self.tail = list(tail)
        self.last_batch = last_batch
        self.completed = completed
        self.in_flight = list(in_flight)
        self.execution_plan = dict(execution_plan) if execution_plan is not None else None
        unsigned = returncode & 0xFFFFFFFF
        super().__init__(
            f"Command failed with exit code {returncode} (0x{unsigned:08X}) after "
            f"{elapsed:.1f}s; see {log_path}"
        )


CommandFn = Callable[
    [Sequence[str], Path, Path, Reporter | None, CommandProgress | None],
    CommandResult | None,
]


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


_LOG_SEVERITY = re.compile(r"\[(?:[^\]]*\s)?(?P<severity>WRN|ERR)\]")


_JRUTIL_PROGRESS_PREFIX = "JRUTIL_PROGRESS "


def run_command(
    command: Sequence[str],
    cwd: Path,
    log_path: Path,
    reporter: Reporter | None = None,
    progress: CommandProgress | None = None,
) -> CommandResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if reporter is not None and progress is not None:
        reporter.stage(progress.label)
    task = reporter.start(progress.label, total=progress.total) if reporter and progress else None
    tail: deque[str] = deque(maxlen=60)
    last_batch: str | None = None
    failed_batch: str | None = None
    completed = 0
    in_flight: set[str] = set()
    maximum_in_flight = 0
    execution_plan: dict[str, object] | None = None
    resource_usage_events: list[dict[str, object]] = []
    capture_metrics: dict[str, object] | None = None
    scheduler_samples: list[dict[str, object]] = []
    maximum_workers_observed = 0
    latest_scheduler_sample: dict[str, object] | None = None
    structured_progress_events = False
    structured_batch_events = False
    current_phase: str | None = None

    def progress_detail() -> str:
        details: list[str] = []
        if latest_scheduler_sample is not None:
            active = latest_scheduler_sample.get("active_workers", "?")
            target = latest_scheduler_sample.get("target_workers", "?")
            cpu = float(cast(float, latest_scheduler_sample.get("normalized_cpu_percent", 0.0)))
            budget = (
                int(cast(int, execution_plan.get("memory_budget_bytes", 0)))
                if execution_plan
                else 0
            )
            private = int(cast(int, latest_scheduler_sample.get("private_bytes", 0)))
            memory = (private / budget * 100.0) if budget else 0.0
            backlog = latest_scheduler_sample.get("completed_backlog", 0)
            details.append(
                f"{active}/{target} workers • CPU {cpu:.0f}% • "
                f"memory {memory:.0f}% • backlog {backlog}"
            )
        elif execution_plan is not None:
            fallback = execution_plan.get("resolved_workers", "?")
            initial = execution_plan.get("initial_workers", fallback)
            maximum = execution_plan.get("maximum_workers", fallback)
            details.append(f"{initial}/{maximum} workers")
        if in_flight:
            details.append(f"{len(in_flight)} active")
        if current_phase:
            details.append(current_phase)
        if last_batch:
            details.append(f"last: {Path(last_batch).name}")
        return " • ".join(details) or "running"

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
            if clean.startswith(_JRUTIL_PROGRESS_PREFIX):
                try:
                    event = cast(
                        dict[str, object],
                        json.loads(clean[len(_JRUTIL_PROGRESS_PREFIX) :]),
                    )
                except (json.JSONDecodeError, TypeError):
                    event = {}
                event_name = event.get("event")
                event_stage = event.get("stage")
                expected_stage = progress.stage if progress is not None else None
                if expected_stage is None or event_stage == expected_stage:
                    structured_progress_events = True
                    if event_name == "execution_plan":
                        execution_plan = event
                        if reporter is not None:
                            budget_gib = int(cast(int, event.get("memory_budget_bytes", 0))) / (
                                1024**3
                            )
                            reporter.note(
                                f"{progress.label if progress else event_stage}: "
                                f"{event.get('resolved_workers')} workers "
                                f"(requested {event.get('requested_jobs')}, "
                                f"CPU {event.get('processor_count')}, "
                                f"memory cap {event.get('memory_limited_jobs')}, "
                                f"budget {budget_gib:.1f} GiB)"
                            )
                            if task is not None:
                                reporter.update(task, completed=completed, detail=progress_detail())
                    elif event_name == "phase":
                        current_phase = str(event.get("name", "running")).replace("-", " ")
                        state = event.get("state")
                        if state == "completed":
                            current_phase = f"{current_phase} done"
                        if reporter is not None and task is not None:
                            reporter.update(task, completed=completed, detail=progress_detail())
                    elif event_name == "resource_usage":
                        resource_usage_events.append(event)
                    elif event_name == "capture_metrics":
                        capture_metrics = event
                    elif event_name == "scheduler_sample":
                        scheduler_samples.append(event)
                        latest_scheduler_sample = event
                        maximum_workers_observed = max(
                            maximum_workers_observed,
                            int(cast(int, event.get("maximum_active_workers", 0))),
                        )
                        if reporter is not None and task is not None:
                            reporter.update(task, completed=completed, detail=progress_detail())
                    elif event_name == "work_progress":
                        phase_name = str(event.get("phase", "running")).replace("-", " ")
                        work_completed = int(cast(int, event.get("completed", 0)))
                        work_total = event.get("total")
                        work_unit = str(event.get("unit", "items"))
                        state = str(event.get("state", "running"))
                        if work_total is None:
                            current_phase = f"{phase_name}: {work_completed} {work_unit}"
                        else:
                            current_phase = (
                                f"{phase_name}: {work_completed}/{int(cast(int, work_total))} "
                                f"{work_unit}"
                            )
                        if state == "completed":
                            current_phase += " done"
                        detail = event.get("detail")
                        if detail:
                            current_phase += f" ({detail})"
                        if reporter is not None and task is not None:
                            reporter.update(task, completed=completed, detail=progress_detail())
                    elif event_name == "batch_started":
                        structured_batch_events = True
                        batch = str(event.get("batch", ""))
                        if batch:
                            in_flight.add(batch)
                            maximum_in_flight = max(maximum_in_flight, len(in_flight))
                        if reporter is not None and task is not None:
                            reporter.update(task, completed=completed, detail=progress_detail())
                    elif event_name == "batch_completed":
                        structured_batch_events = True
                        batch = str(event.get("batch", ""))
                        in_flight.discard(batch)
                        last_batch = batch or last_batch
                        completed += 1
                        if reporter is not None and task is not None:
                            reporter.update(
                                task,
                                completed=completed,
                                detail=progress_detail(),
                            )
                    elif event_name == "batch_failed":
                        structured_batch_events = True
                        batch = str(event.get("batch", ""))
                        in_flight.discard(batch)
                        failed_batch = batch or failed_batch
                        last_batch = failed_batch
                        current_phase = (
                            f"failed: {Path(failed_batch).name}" if failed_batch else "failed"
                        )
                        if reporter is not None and task is not None:
                            reporter.update(task, completed=completed, detail=progress_detail())
            elif (
                not structured_batch_events
                and progress
                and progress.event
                and progress.event in clean
            ):
                last_batch = clean.split(progress.event, 1)[1].strip(" :") or clean
                completed += 1
                if reporter is not None and task is not None:
                    reporter.update(task, completed=completed, detail=progress_detail())
            elif not structured_progress_events and reporter is not None and task is not None:
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
            last_batch=failed_batch or last_batch,
            completed=completed,
            in_flight=sorted(in_flight),
            execution_plan=execution_plan,
        )
    if reporter is not None and task is not None:
        reporter.finish(
            task,
            f"completed in {elapsed:.1f}s"
            + (
                f" with {execution_plan.get('resolved_workers')} workers"
                if execution_plan is not None
                else ""
            ),
        )
    return CommandResult(
        elapsed_seconds=elapsed,
        completed=completed,
        total=progress.total if progress is not None else None,
        execution_plan=execution_plan,
        failed_batch=failed_batch,
        maximum_in_flight=maximum_in_flight,
        resource_usage=tuple(resource_usage_events),
        capture_metrics=capture_metrics,
        scheduler_samples=tuple(scheduler_samples),
        maximum_workers_observed=maximum_workers_observed,
    )


def command_manifest(result: CommandResult | None) -> dict[str, object] | None:
    """The run-manifest record of one supervised command."""

    if result is None:
        return None
    return {
        "elapsed_seconds": result.elapsed_seconds,
        "completed": result.completed,
        "total": result.total,
        "maximum_in_flight": result.maximum_in_flight,
        "execution_plan": result.execution_plan,
        "resource_usage": list(result.resource_usage),
        "capture_metrics": result.capture_metrics,
        "scheduler_samples": list(result.scheduler_samples),
        "maximum_workers_observed": result.maximum_workers_observed,
    }


def failure_record(
    error: BaseException, clock: StageClock, reporter: Reporter, **locations: str
) -> dict[str, Any]:
    """The failure.json record of a failed build, retained with its staging directory."""

    failure: dict[str, Any] = {
        "schema_version": 1,
        "failed_at": utc_now(),
        "stage": clock.current,
        "error_type": type(error).__name__,
        "message": str(error),
        "traceback": traceback.format_exc(),
        "progress": reporter.snapshot(),
        "stage_timings_seconds": clock.timings(include_current=True),
        **locations,
    }
    if isinstance(error, CommandFailure):
        failure["command"] = error.command
        failure["working_directory"] = str(error.cwd)
        failure["exit_code"] = error.returncode
        failure["exit_code_hex"] = f"0x{error.returncode & 0xFFFFFFFF:08X}"
        failure["elapsed_seconds"] = error.elapsed
        failure["last_batch"] = error.last_batch
        failure["completed"] = error.completed
        failure["in_flight_batches"] = error.in_flight
        failure["execution_plan"] = error.execution_plan
        failure["process_log"] = str(error.log_path)
        failure["process_output_tail"] = error.tail
    return failure


def report_failure(
    reporter: Reporter, error: BaseException, stage: str, staging: Path, failure_path: Path
) -> None:
    reporter.problem("error", f"Stage {stage} failed: {error}")
    if isinstance(error, CommandFailure):
        reporter.note(
            f"Last batch: {error.last_batch or 'none reported'}; process log: {error.log_path}"
        )
        if error.tail:
            reporter.note("Last process output:\n" + "\n".join(error.tail[-12:]))
    reporter.note(f"FAILED STAGING RETAINED: {staging}")
    reporter.note(f"Failure report: {failure_path}")
