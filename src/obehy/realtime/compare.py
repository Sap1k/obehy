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
`output` also `feeds`, `gtfs_rt_every_s`, `release` (run ID), `core_version`, `policy_version`,
and only when asked `diagnostics: true` and `quality: true` (below).

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

**`--diagnostics`** (output level) adds what the replay report reads (`replay_report.py`; `obehy
rt report --from-stream` makes `report.json` of such a file), and **`--quality`** (implies it)
also what `quality.py` reads. Journeys are `JourneyKey` objects as in effects; times are
instants; lists of journeys are in the feed's instance order (insertion order, as `refresh`
visits them).

- `poll`, every poll before its step's effects (also a failed or undecodable one): the facts
  level's `poll` record (no `payload_error`), a decoded one also with `feeds`: its observations
  per feed;
- `predictor_sample`, on round multiples of 300 s (at the first tick at or after each, with the
  sample time as `at`), after that tick's effects and journey samples and before its snapshot,
  when the rail feed is replayed: `feed` and `journeys`, each running rail run (with parts)
  having an arrival event or a prediction: `journey`, `arrivals` (`[call index, the arrival
  interval's middle]` of each call with an arrival event) and `predicted` (`[predictor, call
  index, predicted arrival]` per predictor of `policy.PREDICTORS`, in that order, that predicts:
  `timeline.fusion.anchor_prediction`, `propagate` the `sz` target with `current_lateness`,
  only where the call has a scheduled arrival; the arrival is that schedule time plus the
  lateness);
- `journey_sample` (`--quality`), after every tick's effects, per feed in sorted order: `feed`
  and `journeys`, every journey of the feed with `journey`, `trip_id`, `mode`, `lifecycle`,
  `stale`, `lost`, `rail` (has parts), `lead` (has a lead vehicle), `progress` (null or `call`:
  the index reached, `along_m`), `track` (null or `seen_at`, `at_stop`: at a call and not seen
  away, `lateness_s`, `hypotheses`: has any, `off_path`: the best is held off the path), `fix`
  (null unless a track with `seen_at`, a lead vehicle with a position and a path of two points
  or more; else distances from that position: `best_m` to the best hypothesis's point or null,
  `first_m` and `last_m` to the first and last call's point, `near`: the calls `call - 1 ..
  call + 2` within the policy's departure margin), `n` (number of calls), `calls` (`[index,
  call]` of each call that differs from the journey's previous sample, every call on its first
  or when `n` changed; a call: `arrival`, `departure` (the event intervals' middles),
  `estimated_arrival`, `estimated_departure`, `passed_arrival`, `passed_departure`,
  `passenger`, `status`), `plan` (on the first sample and when it changed, else null:
  `scheduled` `[[arrival, departure], ...]`, `zones` `[[arrival_m, departure_m], ...]`,
  `calls_m`, `shaped`, `points`: how many path points). A journey missing from its feed's sample
  is forgotten, so it comes back whole. Floats here are exact (`canonical.EXACT_KINDS`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from google.protobuf import json_format
from google.transit import gtfs_realtime_pb2 as rt

from obehy.realtime.canonical import line
from obehy.realtime.core import CORE_VERSION, estimate_all
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.gtfs_rt import FeedEmitter
from obehy.realtime.emit.recording import RecordingStore
from obehy.realtime.index_sql import file_loads
from obehy.realtime.jsonl import open_lines
from obehy.realtime.manifest import semantics_by_channel
from obehy.realtime.model import Derivation, Effect, FeedState
from obehy.realtime.model_json import facts_to_json
from obehy.realtime.policy import Policy
from obehy.realtime.replay import (
    Clock,
    DecodedPoll,
    ReplayOptions,
    decoded_polls,
    drive,
    finish,
    poll_record,
    samplers,
)
from obehy.realtime.replay import header as stream_header
from obehy.realtime.runner import Runner
from obehy.realtime.times import Instant

Level = Literal["facts", "output"]
Record = dict[str, Any]
Emit = Callable[[Record], None]


class CompareError(RuntimeError):
    """The window cannot be compared (no release for the output level, a broken invariant)."""


def header(options: ReplayOptions, level: Level, policy: Policy) -> Record:
    run_id = file_loads(Path(options.release)).run_id if level == "output" else None
    return stream_header(options, level, policy, run_id)


def facts(options: ReplayOptions, policy: Policy, emit: Emit) -> None:
    """The facts level: every poll of the window decoded (module docstring)."""

    emit(header(options, "facts", policy))
    for decoded in decoded_polls(options, policy):
        entry = decoded.poll.entry
        emit(poll_record(decoded))
        observations = decoded.observations
        if observations is None and decoded.error is None:
            continue
        if observations is None:
            assert decoded.error is not None
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

    diagnostics = options.diagnostics or options.quality
    step = effects("step")

    def stepped(poll: DecodedPoll, produced: list[Effect] | None) -> None:
        if diagnostics:
            emit(poll_record(poll, output=True))
        if produced is not None:
            step(poll.at, produced)

    def snapshot(at: Instant) -> None:
        for _, runtime in sorted(runner.runtimes.items()):
            estimate_all(runtime.state, runtime.ctx, at)
        writer.write_state({feed: r.state for feed, r in runner.runtimes.items()})
        flush(at)
        for _, runtime in sorted(runner.runtimes.items()):
            emit(gtfs_rt(runtime.state, at, policy, writer))

    flush(None)  # what opening the writer writes
    sampled, rail_sampled = samplers(runner, options, emit) if diagnostics else (None, None)
    clock = Clock(
        runner,
        options,
        snapshot=snapshot,
        ticked=effects("tick"),
        sampled=sampled,
        rail_sampled=rail_sampled,
    )
    last = drive(runner, decoded_polls(options, policy), clock, stepped)
    if last is not None:
        produced = finish(runner, last, writer)
        pending = store.take()
        for effect in produced:
            emit({"kind": "effect", "at": last, "phase": "finish", "effect": effect})
        for record in pending:
            emit(record | {"at": last})


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
