"""`obehy rt compare`: the exact record of a replay window that the Rust worker is judged against
(docs/RUST_PORT.md, R0 item 1). Python is the reference; `obehy-rt` writes the same stream from
the same arguments, and `obehy rt compare-diff` (`compare_check.py`) tells where they part.

The window is `obehy rt replay`'s: the same channels, `merged_polls` reception order,
`--since`/`--until` and live feeds unless `--all-feeds` (`replay.decoded_polls`). Output is
JSON lines in the canonical form of `canonical.py` (sorted keys, fixed float decimals by key,
UTC `Z` times; read its docstring: it is the byte-for-byte contract). A path ending in `.gz` is
gzip-compressed with no name and mtime 0, so equal streams give equal files; compare the
decompressed text, not the gzip bytes.

The first record is the `header`: `compare_version`, `level`, `channels` (sorted
`source/channel`), `from`, `to`, `since`, `until`, `all_feeds`, `fact_schema_version`, and for
`output` also `feeds`, `gtfs_rt_every_s`, `release` (run ID), `core_version`, `policy_version`.

**`--level facts`** (connectors only; needs neither a release nor a database). Per archived
poll, in reception order:

- `poll`: `at` (reception), `source`, `channel`, `sha256`, `status`, `error` (the transport
  error archived), `request` (a demand poll's request, else null), `decoder_version`, `outcome`:
  `decoded` (with `observations`: how many after the live-feed filter), `failed` (no body or a
  failed status) or `undecodable`, followed then by `payload_error`: `class` (the exception's
  class name) and `message` (Python's text);
- one `facts` per observation: `item`, `feed`, `observed_at`, `decoder_version` and `facts`:
  the fact JSON of `model_json.facts_to_json` (floats canonical, its own time strings kept). An
  observation names its poll's source, channel, reception time and payload by construction.

**`--level output`** (needs a release directory; no database). Everything the worker would
write, each record tagged with `at` (simulated time), in the order produced:

- `effect`: `phase` (`step`: one poll's observations, at its reception; `tick`: housekeeping
  and estimates; `finish`: the final tick at the last poll) and `effect` (the `model.Effect`,
  generic: `canonical.plain`); a step's or tick's effects first, then the rows the writer made of
  them (`emit/recording.py` lists the row kinds);
- every `gtfs_rt_every_s` (on round multiples, as replay snapshots): estimates for every feed,
  the state tables written (`state` rows, and the vehicle rows that settles), then `gtfs_rt` per
  feed: `feed` and `message`, the FeedMessage as `json_format.MessageToDict` with proto field
  names (64-bit integers as strings, enums by name, float32 as their shortest round-trip
  decimal, then the canonical float rule), vehicles named as the state tables name them;
- after the last poll, the final tick's effects and rows, then the state tables once more.

Alerts and compositions are not part of the stream: alert channels are not replayed (they do
not pass through the core) and compositions need the lookup broker's database.
"""

from __future__ import annotations

import gzip
import io
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Literal

from google.protobuf import json_format
from google.transit import gtfs_realtime_pb2 as rt

from obehy.realtime.canonical import line
from obehy.realtime.core import CORE_VERSION, estimate_all
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.gtfs_rt import FeedEmitter
from obehy.realtime.emit.recording import RecordingStore
from obehy.realtime.facts import FACT_SCHEMA_VERSION
from obehy.realtime.index_sql import file_loads
from obehy.realtime.manifest import semantics_by_channel
from obehy.realtime.model import Derivation, Effect, FeedState
from obehy.realtime.model_json import facts_to_json
from obehy.realtime.policy import Policy
from obehy.realtime.replay import Clock, ReplayOptions, decoded_polls, drive, finish
from obehy.realtime.runner import Runner
from obehy.realtime.sources.registry import DECODER_VERSIONS
from obehy.realtime.times import Instant

COMPARE_VERSION = 1
Level = Literal["facts", "output"]
Record = dict[str, Any]
Emit = Callable[[Record], None]


class CompareError(RuntimeError):
    """The window cannot be compared (no release for the output level, a broken invariant)."""


def header(options: ReplayOptions, level: Level, policy: Policy) -> Record:
    record: Record = {
        "kind": "header",
        "compare_version": COMPARE_VERSION,
        "level": level,
        "channels": sorted(c.name for c in options.channels),
        "from": options.start,
        "to": options.end,
        "since": options.since,
        "until": options.until,
        "all_feeds": options.all_feeds,
        "fact_schema_version": FACT_SCHEMA_VERSION,
    }
    if level == "output":
        record |= {
            "feeds": list(options.feeds),
            "gtfs_rt_every_s": options.gtfs_rt_every_s,
            "release": file_loads(Path(options.release)).run_id,
            "core_version": CORE_VERSION,
            "policy_version": policy.version,
        }
    return record


def facts(options: ReplayOptions, policy: Policy, emit: Emit) -> None:
    """The facts level: every poll of the window decoded (module docstring)."""

    emit(header(options, "facts", policy))
    for decoded in decoded_polls(options, policy):
        entry = decoded.poll.entry
        request = entry.get("request")
        record: Record = {
            "kind": "poll",
            "at": decoded.at,
            "source": decoded.source,
            "channel": decoded.channel,
            "sha256": entry.get("sha256"),
            "status": entry.get("status"),
            "error": entry.get("error"),
            "request": request if isinstance(request, dict) else None,
            "decoder_version": DECODER_VERSIONS[(decoded.source, decoded.channel)],
        }
        observations = decoded.observations
        if observations is not None:
            emit(record | {"outcome": "decoded", "observations": len(observations)})
        elif decoded.error is None:
            emit(record | {"outcome": "failed"})
            continue
        else:
            emit(record | {"outcome": "undecodable"})
            emit(
                {
                    "kind": "payload_error",
                    "at": decoded.at,
                    "class": type(decoded.error).__name__,
                    "message": str(decoded.error),
                }
            )
            continue
        for o in observations:
            identity = (o.source, o.channel, o.received_at, o.raw.sha256)
            if identity != (decoded.source, decoded.channel, decoded.at, entry.get("sha256")):
                raise CompareError(f"an observation of {decoded.source} names another poll")
            emit(
                {
                    "kind": "facts",
                    "at": decoded.at,
                    "item": o.raw.item,
                    "feed": o.feed,
                    "observed_at": o.observed_at,
                    "decoder_version": o.decoder_version,
                    "facts": facts_to_json(o.facts),
                }
            )


def gtfs_rt(state: FeedState, at: Instant, policy: Policy, writer: Writer) -> Record:
    """The `gtfs_rt` record of a feed at `at`, vehicles named as the writer's state rows."""

    payload = FeedEmitter(policy.lifecycle).serialize(state, at, (), writer.vehicles.vehicle_id)
    message = json_format.MessageToDict(
        rt.FeedMessage.FromString(payload), preserving_proto_field_name=True
    )
    return {"kind": "gtfs_rt", "at": at, "feed": state.feed, "message": message}


def output(options: ReplayOptions, policy: Policy, emit: Emit) -> None:
    """The output level: everything the worker would write from the window (module docstring)."""

    if not options.release:
        raise CompareError("the output level needs a release directory (--from-files)")
    loads = file_loads(Path(options.release))
    emit(header(options, "output", policy))
    store = RecordingStore()
    runner = Runner(
        None, loads, policy, options.feeds, semantics=semantics_by_channel(options.channels)
    )
    writer = Writer(store, Derivation(CORE_VERSION, policy.version, loads.run_id))
    runner.writer = writer

    def flush(at: Instant | None) -> None:
        for record in store.take():
            emit(record | {"at": at})

    def effects(phase: str) -> Callable[[Instant, list[Effect]], None]:
        def record(at: Instant, produced: list[Effect]) -> None:
            for effect in produced:
                emit({"kind": "effect", "at": at, "phase": phase, "effect": effect})
            flush(at)

        return record

    def snapshot(at: Instant) -> None:
        for _, runtime in sorted(runner.runtimes.items()):
            estimate_all(runtime.state, runtime.ctx, at)
        writer.write_state({feed: r.state for feed, r in runner.runtimes.items()})
        flush(at)
        for _, runtime in sorted(runner.runtimes.items()):
            emit(gtfs_rt(runtime.state, at, policy, writer))

    flush(None)  # what opening the writer writes
    clock = Clock(runner, options, snapshot=snapshot, ticked=effects("tick"))
    polls = ((d.at, d.observations) for d in decoded_polls(options, policy))
    last = drive(runner, polls, clock, effects("step"))
    if last is not None:
        produced = finish(runner, last, writer)
        pending = store.take()
        for effect in produced:
            emit({"kind": "effect", "at": last, "phase": "finish", "effect": effect})
        for record in pending:
            emit(record | {"at": last})


@contextmanager
def open_lines(path: Path, mode: Literal["r", "w"]) -> Generator[IO[str]]:
    """A JSON-lines file as text, gzip when its name ends in `.gz` (deterministic: no name, mtime
    0)."""

    if path.suffix != ".gz":
        with path.open(mode, encoding="utf-8", newline="\n") as stream:
            yield stream
        return
    with path.open(mode + "b") as raw:
        packed = gzip.GzipFile(filename="", mode=mode + "b", fileobj=raw, mtime=0)
        with packed, io.TextIOWrapper(packed, encoding="utf-8", newline="\n") as stream:
            yield stream


def compare(options: ReplayOptions, level: Level, policy: Policy, out: Path) -> int:
    """Write the window's stream at `level` to `out`; the number of records."""

    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    temporary = out.with_name(".partial-" + out.name)  # keeps the .gz suffix
    with open_lines(temporary, "w") as stream:

        def emit(record: Mapping[str, Any]) -> None:
            nonlocal count
            stream.write(line(record))
            count += 1

        (facts if level == "facts" else output)(options, policy, emit)
    temporary.replace(out)
    return count
