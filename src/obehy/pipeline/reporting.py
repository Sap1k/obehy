"""Build progress reporting (rich, plain or silent)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, cast

from rich.console import Console, RenderableType
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
from rich.table import Column
from rich.text import Text

from obehy.pipeline.files import utc_now

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
        speed = f" {task.speed:.1f}/s" if task.speed else ""
        return Text(f"{int(task.completed)}{total}{speed}")


class _LifecycleSpinnerColumn(SpinnerColumn):
    def render(self, task: Task) -> Text:
        if task.fields.get("lifecycle_finished"):
            return Text("✓", style="green")
        return cast(Text, super().render(task))


class _LifecycleBarColumn(ProgressColumn):
    def __init__(self) -> None:
        super().__init__()
        self._bar = BarColumn(bar_width=24)

    def render(self, task: Task) -> RenderableType:
        if task.fields.get("lifecycle_finished"):
            return Text("")
        return self._bar.render(task)


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
                _LifecycleSpinnerColumn(),
                TextColumn(
                    "[bold]{task.description}",
                    table_column=Column(no_wrap=True),
                ),
                _LifecycleBarColumn(),
                TaskProgressColumn(),
                _MetricColumn(),
                TextColumn(
                    "{task.fields[detail]}",
                    table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"),
                ),
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
                label,
                total=total,
                detail="starting",
                unit=unit,
                lifecycle_finished=False,
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
        self.update(task, completed=state.completed, detail=detail)
        if self._progress is not None:
            rich_task = self._rich_tasks[task]
            self._progress.update(rich_task, lifecycle_finished=True)
            self._progress.stop_task(rich_task)
        elif self.mode == "plain":
            elapsed = time.monotonic() - state.started
            self.note(f"DONE {state.label} ({elapsed:.1f}s): {detail}")

    def problem(self, severity: str, message: str) -> None:
        severity = "error" if severity.lower().startswith("err") else "warning"
        key = (self._stage, severity)
        self._problems[key] = self._problems.get(key, 0) + 1
        if self._problems[key] <= 20:
            style = "bold red" if severity == "error" else "yellow"
            self.console.print(f"{severity.upper()}: {message}", style=style, markup=False)
        else:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1

    def note(self, message: str) -> None:
        self.console.print(f"[{utc_now()}] {message}", markup=False)

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
                    markup=False,
                )


@dataclass(frozen=True)
class CommandProgress:
    label: str
    total: int | None = None
    event: str | None = None
    stage: str | None = None


class StageClock:
    """The current build stage and the wall time accumulated per stage."""

    def __init__(self, initial: str = "initialization") -> None:
        self.current = initial
        self._started = time.monotonic()
        self._seconds: dict[str, float] = {}

    def start(self, name: str) -> None:
        now = time.monotonic()
        self._seconds[self.current] = self._seconds.get(self.current, 0.0) + (now - self._started)
        self.current = name
        self._started = now

    def timings(self, *, include_current: bool = False) -> dict[str, float]:
        seconds = dict(self._seconds)
        if include_current:
            seconds[self.current] = seconds.get(self.current, 0.0) + (
                time.monotonic() - self._started
            )
        return {name: round(value, 3) for name, value in seconds.items()}
