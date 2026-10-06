"""``obehy db`` and ``obehy release`` subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

import psycopg

from obehy.release import activate as activation
from obehy.release.contract import load_contract
from obehy.release.load import PACKAGES, LoadError, load_release
from obehy.release.migrate import MigrationError, migrate
from obehy.runtime_config import ConfigurationError, load_database_url


def add_parsers(commands: Any) -> None:
    database = commands.add_parser("db", help="database administration")
    database_commands = database.add_subparsers(dest="db_command", required=True)
    migrator = database_commands.add_parser("migrate", help="apply pending migrations")
    _connection_arguments(migrator)

    release = commands.add_parser("release", help="load and activate releases in the database")
    release_commands = release.add_subparsers(dest="release_command", required=True)
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
        "--keep",
        type=int,
        default=activation.DEFAULT_KEEP,
        help="predecessor releases whose data stays in the database (default: 2)",
    )
    _connection_arguments(activator)

    reporter = release_commands.add_parser("status", help="show the active release and loads")
    _connection_arguments(reporter)


def _connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database-url", help="overrides OBEHY_DATABASE_URL and the config")


def _connect(args: argparse.Namespace) -> psycopg.Connection:
    url = cast(str | None, args.database_url) or load_database_url(cast(Path | None, args.config))
    return psycopg.connect(url, autocommit=True)


def _report(message: str) -> None:
    print(message, flush=True)


def run(args: argparse.Namespace) -> int:
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
