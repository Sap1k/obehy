"""CLI: `obehy rt record|replay|report|compare|compare-diff|bench|corpus pin`, `obehy realtime`,
`obehy jobs nightly|hourly`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import shutil
import sys
import tomllib
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg

from obehy.realtime import bench, compare, compare_check, record, replay, replay_report
from obehy.realtime.archive import prune as prune_archive
from obehy.realtime.archive import sources as archive_sources
from obehy.realtime.emit.alerts import prune_alerts
from obehy.realtime.facts import FEEDS, Feed
from obehy.realtime.gtfs_rt_check import check
from obehy.realtime.index_sql import IndexLoadError, active_loads
from obehy.realtime.jobs import drop_history, hourly
from obehy.realtime.manifest import Channel, ManifestError, select_channels
from obehy.realtime.policy import PolicyError, load_policy
from obehy.realtime.runtime.demand import StationDemand
from obehy.realtime.runtime.egress import Egress, EgressError, build_egress
from obehy.realtime.runtime.lookups import prune_lookups
from obehy.realtime.sources import registry
from obehy.realtime.times import Instant, local_date, wall_instant
from obehy.realtime.times import now as clock_now
from obehy.realtime.worker import WorkerOptions, run_worker
from obehy.runtime_config import ConfigurationError, load_database_url


def _list(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _feeds(text: str) -> tuple[Feed, ...]:
    chosen = _list(text)
    unknown = sorted(set(chosen) - set(FEEDS))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown feeds {unknown}")
    return tuple(cast(Feed, feed) for feed in chosen)


def _database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database-url", help="overrides OBEHY_DATABASE_URL and the config")


def _url(args: argparse.Namespace) -> str:
    return cast(str | None, args.database_url) or load_database_url(cast(Path | None, args.config))


def add_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    realtime = commands.add_parser("rt", help="realtime tools")
    rt = realtime.add_subparsers(dest="rt_command", required=True)

    recorder = rt.add_parser("record", help="archive realtime payloads without processing them")
    recorder.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    recorder.add_argument("--sources", type=_list, help="comma-separated source IDs")
    recorder.add_argument("--manifest", type=Path, default=record.MANIFEST)
    recorder.add_argument("--once", action="store_true", help="poll every channel once and exit")
    recorder.add_argument("--duration", type=record.parse_duration, help="e.g. 30m, 6h or 2d")
    _database(recorder)  # egress proxy lists; the active release for demand channels

    replayer = rt.add_parser("replay", help="run archived payloads through the realtime core")
    replayer.add_argument(
        "--release", required=True, help="release directory or a run ID already in the database"
    )
    replayer.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    replayer.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    replayer.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    replayer.add_argument(
        "--sources", type=_list, help=f"source IDs (default: {','.join(replay.SOURCES)})"
    )
    replayer.add_argument("--feeds", type=_feeds, default=FEEDS, help="default: jdf,czptt")
    replayer.add_argument("--manifest", type=Path, default=record.MANIFEST)
    replayer.add_argument("--out", type=Path, required=True, help="output directory")
    replayer.add_argument(
        "--write-history", action="store_true", help="rebuild rt.observation and history"
    )
    replayer.add_argument(
        "--gtfs-rt-every", type=int, metavar="SECONDS", help="write GTFS-RT snapshots"
    )
    replayer.add_argument(
        "--since", type=_local_time, help="replay polls from this local time (YYYY-MM-DDTHH:MM)"
    )
    replayer.add_argument("--until", type=_local_time, help="and before this local time")
    replayer.add_argument(
        "--quality",
        action="store_true",
        help="estimate every tick; report prediction accuracy, flicker and dwell at stops",
    )
    replayer.add_argument(
        "--all-feeds",
        action="store_true",
        help="also feeds a channel is not live on yet (live_feeds): to check a connector",
    )
    replayer.add_argument(
        "--from-files",
        action="store_true",
        help="read the release directory's serving files; no database (and no history)",
    )
    _database(replayer)

    comparer = rt.add_parser(
        "compare", help="write a window's exact facts or output stream (the Rust reference)"
    )
    comparer.add_argument("--level", choices=["facts", "output"], required=True)
    comparer.add_argument(
        "--from-files", dest="release", help="release directory (needed by --level output)"
    )
    comparer.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    comparer.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    comparer.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    comparer.add_argument(
        "--sources", type=_list, help=f"source IDs (default: {','.join(replay.SOURCES)})"
    )
    comparer.add_argument("--feeds", type=_feeds, default=FEEDS, help="default: jdf,czptt")
    comparer.add_argument("--manifest", type=Path, default=record.MANIFEST)
    comparer.add_argument("--since", type=_local_time, help="from this local time")
    comparer.add_argument("--until", type=_local_time, help="and before this local time")
    comparer.add_argument("--all-feeds", action="store_true", help="also feeds not live yet")
    comparer.add_argument(
        "--gtfs-rt-every",
        type=int,
        default=60,
        metavar="SECONDS",
        help="state tables and GTFS-RT every so often (output; default 60, 0: only at the end)",
    )
    comparer.add_argument(
        "--diagnostics",
        action="store_true",
        help="output: also the records the replay report reads (poll, predictor_sample)",
    )
    comparer.add_argument(
        "--quality", action="store_true", help="output: also journey_sample (implies --diagnostics)"
    )
    comparer.add_argument("--out", type=Path, required=True, help="JSON lines (.gz: gzip)")
    comparer.add_argument("--check", type=Path, help="then diff against this reference")
    comparer.add_argument("--limit", type=int, default=10, help="differences shown")

    reporter = rt.add_parser(
        "report", help="a replay's report.json from an output stream written with --diagnostics"
    )
    reporter.add_argument("--from-stream", dest="stream", type=Path, required=True)
    reporter.add_argument("--out", type=Path, required=True, help="output directory")

    differ = rt.add_parser("compare-diff", help="where two compare streams part")
    differ.add_argument("reference", type=Path)
    differ.add_argument("candidate", type=Path)
    differ.add_argument("--limit", type=int, default=10, help="differences shown")
    differ.add_argument(
        "--ignore", action="append", default=[], help="KIND.FIELD left out (repeatable)"
    )

    bencher = rt.add_parser(
        "bench", help="time the live worker's path over archived polls (index: database or files)"
    )
    bencher.add_argument(
        "--release", required=True, help="release directory or a run ID already in the database"
    )
    bencher.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    bencher.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    bencher.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    bencher.add_argument("--sources", type=_list, help="source IDs (default: every archived)")
    bencher.add_argument("--feeds", type=_feeds, default=FEEDS, help="default: jdf,czptt")
    bencher.add_argument("--manifest", type=Path, default=record.MANIFEST)
    bencher.add_argument("--out", type=Path, required=True, help="output directory")
    bencher.add_argument("--trace-memory", action="store_true", help="tracemalloc (slow)")
    bencher.add_argument("--profile", action="store_true", help="write a cProfile dump")
    bencher.add_argument(
        "--from-files",
        action="store_true",
        help="read the release directory's serving files; no database",
    )
    bencher.add_argument(
        "--since", type=_local_time, help="start at this local time (YYYY-MM-DDTHH:MM)"
    )
    bencher.add_argument("--until", type=_local_time, help="and stop before this local time")
    _database(bencher)

    checker = rt.add_parser("check-gtfs-rt", help="check GTFS-RT snapshots against a static GTFS")
    checker.add_argument("--gtfs", type=Path, required=True, help="the static gtfs.zip")
    checker.add_argument("snapshots", type=Path, help="a .pb file or a directory of them")

    corpus = rt.add_parser("corpus", help="pinned replay corpora")
    corpus_commands = corpus.add_subparsers(dest="corpus_command", required=True)
    pin = corpus_commands.add_parser("pin", help="copy archive days into a pinned corpus")
    pin.add_argument("name")
    pin.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    pin.add_argument("--pinned", type=Path, required=True, help="pinned corpora root")
    pin.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    pin.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    pin.add_argument("--sources", type=_list, required=True)
    pin.add_argument("--release", required=True, help="run ID the corpus is replayed against")
    pin.add_argument("--reason", required=True)

    worker = commands.add_parser("realtime", help="run the realtime worker")
    worker.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    worker.add_argument("--gtfs-rt", type=Path, default=Path("data/gtfs-rt"))
    worker.add_argument("--sources", type=_list, help="comma-separated source IDs")
    worker.add_argument("--feeds", type=_feeds, default=FEEDS, help="default: jdf,czptt")
    worker.add_argument("--manifest", type=Path, default=record.MANIFEST)
    _database(worker)

    jobs = commands.add_parser("jobs", help="set-wise jobs over history")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    nightly = job_commands.add_parser(
        "nightly",
        help="drop expired history days (with their compositions), alerts, lookups and raw "
        "archive days",
    )
    nightly.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    _database(nightly)
    hourly_jobs = job_commands.add_parser(
        "hourly",
        help="drop expired observation hours; keep the data disk's free space above the guard",
    )
    hourly_jobs.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    hourly_jobs.add_argument(
        "--data", type=Path, default=Path("data"), help="where to measure the free space"
    )
    _database(hourly_jobs)


def run(args: argparse.Namespace) -> int:
    try:
        if args.command == "realtime":
            return _worker(args)
        if args.command == "jobs":
            return _jobs(args)
        if args.rt_command == "record":
            return _record(args)
        if args.rt_command == "corpus":
            return _pin(args)
        if args.rt_command == "check-gtfs-rt":
            return _check(args)
        if args.rt_command == "bench":
            return _bench(args)
        if args.rt_command == "compare":
            return _compare(args)
        if args.rt_command == "compare-diff":
            return _compare_diff(args)
        if args.rt_command == "report":
            return _report(args)
        return _replay(args)
    except (
        OSError,
        ManifestError,
        PolicyError,
        ConfigurationError,
        IndexLoadError,
        replay.ReplayError,
        compare.CompareError,
        replay_report.ReportError,
        EgressError,
        psycopg.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _egress(
    channels: list[Channel], args: argparse.Namespace, lookups: Sequence[Channel] = ()
) -> tuple[list[Channel], Egress]:
    """The channels that may poll and their fetcher (of lookups' requests too): egress channels
    only through their proxy pool, and not at all without one (docs/R2_SLICE.md section 7)."""

    egress = build_egress(
        [*channels, *lookups],
        load_policy().egress,
        config=cast(Path | None, args.config),
        log=record.log,
    )
    usable = egress.usable(channels, record.log)
    if not usable:
        raise EgressError(
            "no channel can poll: configure the egress proxy lists and source secrets"
        )
    return usable, egress


def _demand(
    channels: list[Channel], args: argparse.Namespace
) -> tuple[list[Channel], StationDemand | None]:
    """Demand channels need the active release's due calls; without a database they are left
    out of a recording."""

    if not any(channel.demand is not None for channel in channels):
        return channels, None
    try:
        connection = psycopg.connect(_url(args), autocommit=True)
        loads = active_loads(connection)
    except (ConfigurationError, IndexLoadError, psycopg.Error) as error:
        record.log(f"demand channels not recorded (no active release in a database: {error})")
        return [channel for channel in channels if channel.demand is None], None
    return channels, StationDemand(connection, loads.load_ids["czptt"], load_policy().boards)


def _record(args: argparse.Namespace) -> int:
    channels, egress = _egress(
        select_channels(
            record.load_channels(cast(Path, args.manifest)), cast(list[str] | None, args.sources)
        ),
        args,
    )
    channels, demand = _demand(channels, args)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            record.record(
                channels,
                cast(Path, args.archive),
                once=cast(bool, args.once),
                duration_s=cast(float | None, args.duration),
                fetcher=egress.fetch,
                body_fetcher=egress.fetch,
                demand=None if demand is None else demand.due,
                boards=load_policy().boards,
            )
        )
    return 0


def _local_time(value: str) -> Instant:
    """A Prague wall-clock time from the command line (an explicit offset is kept)."""

    return wall_instant(datetime.fromisoformat(value))


def _replay_channels(args: argparse.Namespace) -> list[Channel]:
    return [
        channel
        for channel in select_channels(
            record.load_channels(cast(Path, args.manifest)),
            cast(list[str] | None, args.sources) or list(replay.SOURCES),
        )
        if (channel.source, channel.channel) not in registry.ALERT_DECODERS
    ]


def _compare(args: argparse.Namespace) -> int:
    level = cast(compare.Level, args.level)
    release = cast(str | None, args.release)
    if level == "output" and not release:
        print("error: --level output needs --from-files <release dir>", file=sys.stderr)
        return 1
    every = cast(int, args.gtfs_rt_every)
    options = replay.ReplayOptions(
        release=release or "",
        archive=cast(Path, args.archive),
        start=cast(date, args.start),
        end=cast(date, args.end),
        channels=_replay_channels(args),
        out=cast(Path, args.out).parent,
        feeds=cast(tuple[Feed, ...], args.feeds),
        gtfs_rt_every_s=every or None,
        since=cast(Instant | None, args.since),
        until=cast(Instant | None, args.until),
        all_feeds=cast(bool, args.all_feeds),
        quality=cast(bool, args.quality),
        diagnostics=cast(bool, args.diagnostics),
    )
    out = cast(Path, args.out)
    count = compare.compare(options, level, load_policy(), out)
    print(f"{count} records in {out}")
    reference = cast(Path | None, args.check)
    if reference is None:
        return 0
    same, lines = compare_check.diff(reference, out, limit=cast(int, args.limit))
    print("\n".join(lines))
    return 0 if same else 1


def _compare_diff(args: argparse.Namespace) -> int:
    same, lines = compare_check.diff(
        cast(Path, args.reference),
        cast(Path, args.candidate),
        limit=cast(int, args.limit),
        ignore=cast(list[str], args.ignore),
    )
    print("\n".join(lines))
    return 0 if same else 1


def _replay(args: argparse.Namespace) -> int:
    channels = _replay_channels(args)
    options = replay.ReplayOptions(
        release=cast(str, args.release),
        archive=cast(Path, args.archive),
        start=cast(date, args.start),
        end=cast(date, args.end),
        channels=channels,
        out=cast(Path, args.out),
        feeds=cast(tuple[Feed, ...], args.feeds),
        write_history=cast(bool, args.write_history),
        gtfs_rt_every_s=cast(int | None, args.gtfs_rt_every),
        quality=cast(bool, args.quality),
        since=cast(Instant | None, args.since),
        until=cast(Instant | None, args.until),
        all_feeds=cast(bool, args.all_feeds),
    )
    if cast(bool, args.from_files):
        document = replay.replay(None, options, load_policy())
    else:
        with psycopg.connect(_url(args), autocommit=True) as connection:
            document = replay.replay(connection, options, load_policy())
    for line in replay_report.summary_lines(document):
        print(line)
    return 0


def _report(args: argparse.Namespace) -> int:
    document = replay_report.from_stream(
        cast(Path, args.stream), cast(Path, args.out), load_policy().keyless.episode_gap_s
    )
    for line in replay_report.summary_lines(document):
        print(line)
    return 0


def _bench(args: argparse.Namespace) -> int:
    archive = cast(Path, args.archive)
    channels = [
        channel
        for channel in select_channels(
            record.load_channels(cast(Path, args.manifest)), cast(list[str] | None, args.sources)
        )
        if (channel.source, channel.channel) in registry.DECODERS
        and (archive / channel.source / channel.channel).is_dir()
    ]
    if not channels:
        print(f"error: no decodable channel is archived in {archive}", file=sys.stderr)
        return 1
    options = bench.BenchOptions(
        release=cast(str, args.release),
        archive=archive,
        start=cast(date, args.start),
        end=cast(date, args.end),
        channels=channels,
        out=cast(Path, args.out),
        feeds=cast(tuple[Feed, ...], args.feeds),
        trace_memory=cast(bool, args.trace_memory),
        profile=cast(bool, args.profile),
        since=cast(Instant | None, args.since),
        until=cast(Instant | None, args.until),
    )
    if cast(bool, args.from_files):
        document = bench.bench(None, options, load_policy())
    else:
        with psycopg.connect(_url(args), autocommit=True) as connection:
            document = bench.bench(connection, options, load_policy())
    for line in bench.summary_lines(document):
        print(line)
    return 0


def _worker(args: argparse.Namespace) -> int:
    sources = cast(list[str] | None, args.sources)
    lookups = [
        lookup
        for lookup in record.load_lookups(cast(Path, args.manifest))
        if not sources or lookup.source in sources
    ]
    channels, egress = _egress(
        select_channels(record.load_channels(cast(Path, args.manifest)), sources),
        args,
        [lookup.request for lookup in lookups],
    )
    lookups = [lookup for lookup in lookups if egress.usable([lookup.request], record.log)]
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            run_worker(
                _url(args),
                channels,
                load_policy(),
                WorkerOptions(
                    archive=cast(Path, args.archive),
                    gtfs_rt_dir=cast(Path, args.gtfs_rt),
                    feeds=cast(tuple[Feed, ...], args.feeds),
                    fetcher=egress.fetch,
                    body_fetcher=egress.fetch,
                    lookups=lookups,
                ),
            )
        )
    return 0


def _jobs(args: argparse.Namespace) -> int:
    policy = load_policy()
    now = clock_now()
    if args.job_command == "hourly":
        data = cast(Path, args.data)
        with psycopg.connect(_url(args), autocommit=True) as connection:
            for line in hourly(
                connection,
                cast(Path, args.archive),
                policy,
                now,
                lambda: shutil.disk_usage(data).free,
            ):
                print(line, file=sys.stderr if line.startswith("disk guard") else sys.stdout)
        return 0
    with psycopg.connect(_url(args), autocommit=True) as connection:
        kept = local_date(now) - timedelta(days=policy.retention.history_days)
        dropped = drop_history(connection, kept)
        print(f"dropped {len(dropped)} history days before {kept}")
        # Alerts and lookup requests outlive the raw archive's shorter windows.
        withdrawn = now - timedelta(days=policy.retention.longest_raw_days)
        print(f"deleted {prune_alerts(connection, withdrawn)} alerts withdrawn before {withdrawn}")
        print(f"deleted {prune_lookups(connection, withdrawn)} lookup requests before {withdrawn}")
    archive = cast(Path, args.archive)
    for source in archive_sources(archive):
        cutoff = now.date() - timedelta(days=int(policy.retention.raw_days(source)))
        pruned = prune_archive(archive, cutoff, source)
        print(f"deleted {len(pruned)} raw archive days of {source} before {cutoff}")
    return 0


def _check(args: argparse.Namespace) -> int:
    target = cast(Path, args.snapshots)
    files = sorted(target.glob("*.pb")) if target.is_dir() else [target]
    problems, totals = check(cast(Path, args.gtfs), files)
    print(", ".join(f"{count} {name}" for name, count in sorted(totals.items())))
    for name, count in sorted(problems.items()):
        print(f"problem: {count} x {name}")
    return 1 if problems else 0


def _pin(args: argparse.Namespace) -> int:
    """Copy archive days into `<pinned>/<name>/` with a `corpus.toml` describing them."""

    root = cast(Path, args.pinned) / cast(str, args.name)
    archive = cast(Path, args.archive)
    start, end = cast(date, args.start), cast(date, args.end)
    sources = cast(list[str], args.sources)
    copied = 0
    for source in sources:
        for channel_dir in sorted((archive / source).glob("*")):
            day = start
            while day <= end:
                src = channel_dir / day.isoformat()
                if src.is_dir():
                    dst = root / "archive" / source / channel_dir.name / day.isoformat()
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    copied += 1
                day += timedelta(days=1)
    if not copied:
        print(f"error: no archive days of {sources} between {start} and {end}", file=sys.stderr)
        return 1
    manifest = root / "corpus.toml"
    manifest.write_text(
        "\n".join(
            [
                f'name = "{args.name}"',
                f'release = "{args.release}"',
                f'from = "{start.isoformat()}"',
                f'to = "{end.isoformat()}"',
                "sources = [" + ", ".join(f'"{s}"' for s in sources) + "]",
                f"reason = {_toml_string(cast(str, args.reason))}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    tomllib.loads(manifest.read_text(encoding="utf-8"))
    print(f"pinned {copied} archive days into {root}")
    return 0


def _toml_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
