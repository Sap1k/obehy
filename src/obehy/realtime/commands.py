"""CLI: `obehy rt record|replay|corpus pin`, `obehy realtime`, `obehy jobs vehicle-day`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import shutil
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import psycopg

from obehy.realtime import record, replay
from obehy.realtime.gtfs_rt_check import check
from obehy.realtime.index_sql import IndexLoadError
from obehy.realtime.jobs import vehicle_day
from obehy.realtime.manifest import ManifestError, select_channels
from obehy.realtime.model import FEEDS, Feed
from obehy.realtime.policy import PolicyError, load_policy
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
    replayer.add_argument("--feeds", type=_feeds, default=("jdf",), help="default: jdf")
    replayer.add_argument("--manifest", type=Path, default=record.MANIFEST)
    replayer.add_argument("--out", type=Path, required=True, help="output directory")
    replayer.add_argument(
        "--write-history", action="store_true", help="rebuild rt.observation and history"
    )
    replayer.add_argument(
        "--gtfs-rt-every", type=int, metavar="SECONDS", help="write GTFS-RT snapshots"
    )
    _database(replayer)

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
    worker.add_argument("--feeds", type=_feeds, default=("jdf",), help="default: jdf")
    worker.add_argument("--manifest", type=Path, default=record.MANIFEST)
    _database(worker)

    jobs = commands.add_parser("jobs", help="set-wise jobs over history")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    days = job_commands.add_parser("vehicle-day", help="rebuild vehicle days of service dates")
    days.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    days.add_argument("--to", dest="end", type=date.fromisoformat)
    _database(days)


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
        return _replay(args)
    except (
        OSError,
        ManifestError,
        PolicyError,
        ConfigurationError,
        IndexLoadError,
        replay.ReplayError,
        psycopg.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _record(args: argparse.Namespace) -> int:
    channels = select_channels(
        record.load_channels(cast(Path, args.manifest)), cast(list[str] | None, args.sources)
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            record.record(
                channels,
                cast(Path, args.archive),
                once=cast(bool, args.once),
                duration_s=cast(float | None, args.duration),
            )
        )
    return 0


def _replay(args: argparse.Namespace) -> int:
    channels = select_channels(
        record.load_channels(cast(Path, args.manifest)),
        cast(list[str] | None, args.sources) or list(replay.SOURCES),
    )
    options = replay.ReplayOptions(
        release=cast(str, args.release),
        archive=cast(Path, args.archive),
        start=cast(date, args.start),
        end=cast(date, args.end),
        channels=[(c.source, c.channel) for c in channels],
        out=cast(Path, args.out),
        feeds=cast(tuple[Feed, ...], args.feeds),
        write_history=cast(bool, args.write_history),
        gtfs_rt_every_s=cast(int | None, args.gtfs_rt_every),
    )
    with psycopg.connect(_url(args), autocommit=True) as connection:
        document = replay.replay(connection, options, load_policy())
    for line in replay.summary_lines(document):
        print(line)
    return 0


def _worker(args: argparse.Namespace) -> int:
    channels = select_channels(
        record.load_channels(cast(Path, args.manifest)), cast(list[str] | None, args.sources)
    )
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
                ),
            )
        )
    return 0


def _jobs(args: argparse.Namespace) -> int:
    start = cast(date, args.start)
    end = cast(date | None, args.end) or start
    policy = load_policy()
    with psycopg.connect(_url(args), autocommit=True) as connection:
        day = start
        while day <= end:
            print(f"{day}: {vehicle_day(connection, day, policy)} vehicle days")
            day += timedelta(days=1)
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
