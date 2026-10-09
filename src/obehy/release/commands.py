"""``obehy db`` and ``obehy release`` subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

import psycopg

from obehy.release import activate as activation
from obehy.release import fetch as fetching
from obehy.release.contract import load_contract
from obehy.release.load import PACKAGES, LoadError, load_release
from obehy.release.migrate import MigrationError, migrate
from obehy.runtime_config import ConfigurationError, default_config_path, load_database_url

DEFAULT_RELEASES = default_config_path().parents[1] / "data" / "releases"


def add_parsers(commands: Any) -> None:
    database = commands.add_parser("db", help="database administration")
    database_commands = database.add_subparsers(dest="db_command", required=True)
    migrator = database_commands.add_parser("migrate", help="apply pending migrations")
    _connection_arguments(migrator)

    release = commands.add_parser("release", help="load and activate releases in the database")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    fetcher = release_commands.add_parser(
        "fetch",
        help="download, verify and unpack a published release; prints its directory",
    )
    fetcher.add_argument("run_id", nargs="?", help="the run to fetch (default: the newest build)")
    fetcher.add_argument(
        "--into",
        type=Path,
        default=DEFAULT_RELEASES,
        help="directory of fetched releases (default: data/releases)",
    )
    fetcher.add_argument("--repository", default=fetching.DEFAULT_REPOSITORY)
    fetcher.add_argument(
        "--keep",
        type=int,
        default=fetching.DEFAULT_KEEP,
        help="fetched release directories to keep, newest first (default: 3)",
    )

    loader = release_commands.add_parser("load", help="verify and load a release directory")
    loader.add_argument("release_dir", type=Path)
    loader.add_argument(
        "--package",
        action="append",
        choices=PACKAGES,
        help="load only this package (repeatable; default: all)",
    )
    loader.add_argument("--reload", action="store_true", help="load again even if already loaded")
    _connection_arguments(loader)

    activator = release_commands.add_parser("activate", help="activate a loaded release")
    target = activator.add_mutually_exclusive_group(required=True)
    target.add_argument("run_id", nargs="?")
    target.add_argument(
        "--rollback", action="store_true", help="return to the previously active release"
    )
    activator.add_argument(
        "--if-newer",
        action="store_true",
        help="do nothing unless the release is newer than every release activated before",
    )
    activator.add_argument(
        "--keep",
        type=int,
        default=activation.DEFAULT_KEEP,
        help="predecessor releases whose data stays in the database (default: 2)",
    )
    _connection_arguments(activator)

    reporter = release_commands.add_parser("status", help="show the active release and loads")
    _connection_arguments(reporter)


def _fetch(args: argparse.Namespace) -> int:
    into = cast(Path, args.into)
    try:
        published = fetching.find_release(cast(str, args.repository), cast(str | None, args.run_id))
        directory, new = fetching.fetch_release(published, into, report=_progress)
    except (fetching.FetchError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    _progress(f"fetched {published.run_id}" if new else f"{published.run_id} is already fetched")
    for run in fetching.prune(into, cast(int, args.keep), published.run_id):
        _progress(f"deleted the fetched release {run}")
    print(directory)
    return 0


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database-url", help="overrides OBEHY_DATABASE_URL and the config")


def _connect(args: argparse.Namespace) -> psycopg.Connection:
    url = cast(str | None, args.database_url) or load_database_url(cast(Path | None, args.config))
    return psycopg.connect(url, autocommit=True)


def _report(message: str) -> None:
    print(message, flush=True)


def run(args: argparse.Namespace) -> int:
    if args.command == "release" and args.release_command == "fetch":
        return _fetch(args)
    try:
        with _connect(args) as connection:
            if args.command == "db":
                applied = migrate(connection)
                for migration in applied:
                    print(f"applied {migration.version:04d}_{migration.name}")
                if not applied:
                    print("database is up to date")
                return 0
            contract = load_contract()
            if args.release_command == "load":
                load_release(
                    connection,
                    cast(Path, args.release_dir),
                    contract,
                    packages=cast(list[str] | None, args.package) or PACKAGES,
                    reload=cast(bool, args.reload),
                    report=_report,
                )
                return 0
            if args.release_command == "activate":
                if args.rollback:
                    publication = activation.rollback(connection, contract)
                    verb = "rolled back to"
                elif args.if_newer:
                    run_id = cast(str, args.run_id)
                    activated = activation.activate_if_newer(connection, contract, run_id)
                    if activated is None:
                        print(f"{run_id} is not newer than the releases activated before")
                        return 0
                    publication, verb = activated, "activated"
                else:
                    publication = activation.activate(connection, contract, cast(str, args.run_id))
                    verb = "activated"
                print(f"{verb} {publication.run_id} (loads {publication.load_ids})")
                dropped = activation.prune(connection, contract, cast(int, args.keep))
                if dropped:
                    print(f"dropped the data of loads {dropped}")
                return 0
            print(json.dumps(activation.status(connection), indent=2, default=str))
            return 0
    except (
        ConfigurationError,
        MigrationError,
        LoadError,
        activation.ActivationError,
        psycopg.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
