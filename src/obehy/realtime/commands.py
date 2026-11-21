"""CLI: `obehy rt report|check-gtfs-rt|corpus pin` and `obehy jobs nightly|hourly`.

The realtime worker is the Rust binary `obehy-rt` (docs/RUST_PORT.md): `obehy-rt worker`, and
its `record`, `replay`, `warm`, `bench` and `compare`. What stays here reads what it leaves: its
output stream, its GTFS-RT files, its raw archive and the database."""

from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg

from obehy.realtime import replay_report
from obehy.realtime.gtfs_rt_check import check
from obehy.realtime.jobs import hourly, nightly
from obehy.realtime_config import ConfigError, instant, policy_values
from obehy.runtime_config import ConfigurationError, load_database_url


def _list(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database-url", help="overrides OBEHY_DATABASE_URL and the config")


def _url(args: argparse.Namespace) -> str:
    return cast(str | None, args.database_url) or load_database_url(cast(Path | None, args.config))


def add_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    realtime = commands.add_parser(
        "rt", help="realtime tools over the worker's output (the worker is obehy-rt)"
    )
    rt = realtime.add_subparsers(dest="rt_command", required=True)

    reporter = rt.add_parser(
        "report",
        help="a replay's report.json from an output stream (obehy-rt compare --level output"
        " --diagnostics)",
    )
    reporter.add_argument("--from-stream", dest="stream", type=Path, required=True)
    reporter.add_argument("--out", type=Path, required=True, help="output directory")

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

    jobs = commands.add_parser("jobs", help="set-wise jobs over history")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    nightly_jobs = job_commands.add_parser(
        "nightly",
        help="drop expired history days (with their compositions), alerts, lookups and raw "
        "archive days",
    )
    nightly_jobs.add_argument("--archive", type=Path, default=Path("data/rt-raw"))
    _database(nightly_jobs)
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
        if args.command == "jobs":
            return _jobs(args)
        if args.rt_command == "corpus":
            return _pin(args)
        if args.rt_command == "check-gtfs-rt":
            return _check(args)
        return _report(args)
    except (
        OSError,
        ConfigError,
        ConfigurationError,
        replay_report.ReportError,
        psycopg.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _report(args: argparse.Namespace) -> int:
    document = replay_report.from_stream(
        cast(Path, args.stream), cast(Path, args.out), policy_values().keyless_episode_gap_s
    )
    for line in replay_report.summary_lines(document):
        print(line)
    return 0


def _jobs(args: argparse.Namespace) -> int:
    policy = policy_values()
    now = instant(datetime.now(UTC))
    archive = cast(Path, args.archive)
    with psycopg.connect(_url(args), autocommit=True) as connection:
        if args.job_command == "hourly":
            data = cast(Path, args.data)
            for line in hourly(
                connection, archive, policy, now, lambda: shutil.disk_usage(data).free
            ):
                print(line, file=sys.stderr if line.startswith("disk guard") else sys.stdout)
        else:
            for line in nightly(connection, archive, policy, now):
                print(line)
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
    """Copy archive days into `<pinned>/<name>/` with a `corpus.toml` describing them (the raw
    archive's layout is the Rust worker's, unchanged: `<root>/<source>/<channel>/<day>/`)."""

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
