"""`obehy rt bench`: the live worker's per-poll and per-tick path over archived polls.

Archived polls of the chosen channels are fed, in reception order, to the worker's own
`PollHandler` (archive, decode, core) with the emit tick (estimates, GTFS-RT) on a simulated
clock. Nothing is written to the database, which is only read for the index: a development
database's round trips would swamp the CPU being measured. Unlike replay,
which skips emitting, this is the work the live worker does, so it measures that:

- CPU and wall time per phase, also as a share of one core over the simulated time;
- every simulated hour: the process's resident memory, Python's traced memory (with
  `trace_memory`), and the sizes of the long-lived containers (index, plan cache, state).

The payloads are archived after the recorder's filters, so filter costs are not measured.
"""

from __future__ import annotations

import cProfile
import ctypes
import json
import sys
import time
import tracemalloc
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.archive import ArchivedPoll, ArchiveWriter, Poll
from obehy.realtime.index_sql import ensure_release
from obehy.realtime.manifest import Channel, semantics_by_channel
from obehy.realtime.model import Feed
from obehy.realtime.policy import Policy
from obehy.realtime.replay import merged_polls
from obehy.realtime.runner import Runner
from obehy.realtime.times import Instant, instant
from obehy.realtime.worker import PollHandler

SAMPLE_EVERY = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class BenchOptions:
    release: str | Path
    archive: Path
    start: date
    end: date
    channels: Sequence[Channel]
    out: Path
    feeds: tuple[Feed, ...] = ("jdf", "czptt")
    trace_memory: bool = False
    profile: bool = False
    since: Instant | None = None
    until: Instant | None = None


def rss_bytes() -> int | None:
    """The process's resident memory, where the platform tells."""

    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        windll: Any = getattr(ctypes, "windll")  # noqa: B009
        kernel32 = windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Counters),
            ctypes.c_ulong,
        ]
        process = kernel32.GetCurrentProcess()
        if not kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize)
    try:
        pages = Path("/proc/self/statm").read_text().split()[1]
    except OSError:
        return None
    import os

    return int(pages) * os.sysconf("SC_PAGE_SIZE")


def sizes(runner: Runner) -> dict[str, int]:
    """The sizes of the containers that live as long as the worker, summed over feeds."""

    out: dict[str, int] = {}

    def add(name: str, value: int) -> None:
        out[name] = out.get(name, 0) + value

    for runtime in runner.runtimes.values():
        index = runtime.loader.index
        add("index_keys", len(index.key_entries))
        add("index_absent_keys", sum(1 for entries in index.key_entries.values() if not entries))
        add("index_trips", len(index.trips))
        add("index_calls", sum(len(trip.calls) for trip in index.trips.values()))
        add("index_shapes", len(index.shapes))
        add("index_shape_points", sum(len(shape.points) for shape in index.shapes.values()))
        add("index_dates", len(index.loaded_dates))
        add("plan_paths", len(runtime.ctx.plans.paths))
        add("plans", len(runtime.ctx.plans.plans))
        state = runtime.state
        add("vehicles", len(state.vehicles))
        add("instances", len(state.instances))
        longest = 0
        placements = 0
        for journey in state.instances.values():
            if journey.track is None:
                continue
            for hypothesis in journey.track.hypotheses:
                placements += len(hypothesis.history)
                longest = max(longest, len(hypothesis.history))
        add("history_placements", placements)
        out["history_longest"] = max(out.get("history_longest", 0), longest)
    return out


def _poll(archived: ArchivedPoll) -> Poll:
    entry = archived.entry
    received = archived.received_at
    elapsed = entry.get("elapsed_ms")
    request = entry.get("request")
    headers = entry.get("headers")
    status = entry.get("status")
    return Poll(
        requested_at=received - timedelta(milliseconds=elapsed if isinstance(elapsed, int) else 0),
        received_at=received,
        status=status if isinstance(status, int) else None,
        body=archived.body(),
        content_type=entry.get("content_type"),
        headers=cast(dict[str, str], headers) if isinstance(headers, dict) else {},
        error=entry.get("error"),
        request=cast(dict[str, str], request) if isinstance(request, dict) else {},
    )


class _Bench:
    def __init__(self, runner: Runner, handler: PollHandler, options: BenchOptions) -> None:
        self.runner = runner
        self.handler = handler
        self.options = options
        self.tick = timedelta(seconds=runner.policy.emit_tick_s)
        self.first: Instant | None = None
        self.next_tick: Instant | None = None
        self.next_sample: Instant | None = None
        self.samples: list[dict[str, Any]] = []
        self.polls = 0

    def advance(self, at: Instant) -> None:
        if self.first is None:
            self.first = at
            self.next_tick = instant(at + self.tick)
            self.next_sample = instant(at + SAMPLE_EVERY)
        assert self.next_tick is not None and self.next_sample is not None
        while self.next_tick <= at:
            self.runner.tick(self.next_tick)
            self.next_tick = instant(self.next_tick + self.tick)
        if self.next_sample <= at:
            self.sample(self.next_sample)
            self.next_sample = instant(self.next_sample + SAMPLE_EVERY)

    def sample(self, at: Instant) -> None:
        assert self.first is not None
        phases = self.runner.phases
        traced = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else None
        self.samples.append(
            {
                "at": at.isoformat(),
                "simulated_h": round((at - self.first) / timedelta(hours=1), 2),
                "polls": self.polls,
                "rss_mb": None if (rss := rss_bytes()) is None else round(rss / 2**20, 1),
                "traced_mb": None if traced is None else round(traced / 2**20, 1),
                "cpu_s": round(sum(phases.cpu_s.values()), 2),
                "wall_s": round(sum(phases.wall_s.values()), 2),
                **sizes(self.runner),
            }
        )


def bench(
    connection: psycopg.Connection,
    options: BenchOptions,
    policy: Policy,
    *,
    report: Callable[[str], None] = print,
) -> dict[str, Any]:
    loads = ensure_release(connection, options.release, report=report)
    channels = {(c.source, c.channel): c for c in options.channels}
    out = options.out
    (out / "gtfs-rt").mkdir(parents=True, exist_ok=True)
    runner = Runner(
        connection,
        loads,
        policy,
        options.feeds,
        None,  # CPU only: no history or state writes (the database is read for the index)
        out / "gtfs-rt",
        semantics_by_channel(options.channels),
    )
    handler = PollHandler(runner, ArchiveWriter(out / "archive"), policy)
    # The archived body is already filtered: the handler stores it as it is.
    unfiltered = {key: replace(channel, filter=None) for key, channel in channels.items()}
    state = _Bench(runner, handler, options)
    if options.trace_memory:
        tracemalloc.start()
    profiler = cProfile.Profile() if options.profile else None
    started = time.perf_counter()
    last: Instant | None = None
    if profiler is not None:
        profiler.enable()
    try:
        for received, source, name, archived in merged_polls(
            options.archive, list(channels), options.start, options.end
        ):
            at = instant(received)
            if options.since is not None and at < options.since:
                continue
            if options.until is not None and at >= options.until:
                break
            state.advance(at)
            handler.handle(unfiltered[(source, name)], _poll(archived))
            state.polls += 1
            last = at
    finally:
        if profiler is not None:
            profiler.disable()
    elapsed = time.perf_counter() - started
    if last is not None:
        state.sample(last)
    if profiler is not None:
        profiler.dump_stats(out / "profile.pstats")
    if tracemalloc.is_tracing():
        top = tracemalloc.take_snapshot().statistics("lineno")[:25]
        tracemalloc.stop()
        (out / "tracemalloc.txt").write_text("\n".join(str(s) for s in top) + "\n", "utf-8")
    simulated_s = (
        0.0 if last is None or state.first is None else (last - state.first).total_seconds()
    )
    phases = runner.phases
    document: dict[str, Any] = {
        "release": loads.run_id,
        "from": options.start.isoformat(),
        "to": options.end.isoformat(),
        "channels": sorted(f"{s}/{c}" for s, c in channels),
        "feeds": list(options.feeds),
        "polls": state.polls,
        "simulated_s": simulated_s,
        "elapsed_s": round(elapsed, 1),
        "phases": {
            name: {
                "cpu_s": round(phases.cpu_s[name], 2),
                "wall_s": round(phases.wall_s[name], 2),
                "calls": phases.calls[name],
                "core_share_cpu": _share(phases.cpu_s[name], simulated_s),
            }
            for name in sorted(phases.wall_s, key=lambda n: -phases.wall_s[n])
        },
        "core_share_cpu": _share(sum(phases.cpu_s.values()), simulated_s),
        "core_share_wall": _share(sum(phases.wall_s.values()), simulated_s),
        "samples": state.samples,
    }
    with atomic_output_path(out / "bench.json") as temporary:
        temporary.write_text(json.dumps(document, indent=2) + "\n", "utf-8")
    return document


def _share(seconds: float, over_s: float) -> float | None:
    return round(seconds / over_s, 4) if over_s else None


def summary_lines(document: dict[str, Any]) -> list[str]:
    hours = document["simulated_s"] / 3600
    lines = [
        f"{document['polls']} polls over {hours:.1f} simulated h in {document['elapsed_s']} s;"
        f" {100 * (document['core_share_cpu'] or 0):.1f}% of a core CPU,"
        f" {100 * (document['core_share_wall'] or 0):.1f}% wall"
    ]
    for name, phase in document["phases"].items():
        lines.append(
            f"  {name:<8} cpu {phase['cpu_s']:>9.2f} s  wall {phase['wall_s']:>9.2f} s"
            f"  ({phase['calls']} calls)"
        )
    if document["samples"]:
        first, final = document["samples"][0], document["samples"][-1]
        for key in ("rss_mb", "traced_mb", "index_trips", "plan_paths", "instances"):
            lines.append(
                f"  {key}: {first.get(key)} at {first['simulated_h']} h -> {final.get(key)}"
            )
    return lines
