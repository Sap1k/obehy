"""``obehy ref`` subcommands: import curated reference datasets into `ref.*`, and fetch the
ones built from a source (PID's vehicle models)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psycopg

from obehy.reference import duk_register, pid_vehicles, vehicle_list
from obehy.reference.dataset import Dataset
from obehy.reference.load import load_dataset
from obehy.reference.registry import REGISTRY_CHANNEL
from obehy.runtime_config import (
    ConfigurationError,
    load_database_url,
    load_reference_path,
    load_secrets,
)

# Dataset name -> its reader: our own vehicle list (`vehicles`) and the DÚK register
# (`duk-register`), both curated in jrunify-ext-geodata/vehicles/, and PID's models
# (`pid-vehicles`), built from Golemio by `ref fetch`. A file comes from `--path` or
# `[reference] <name>` in the config.
READERS: dict[str, Callable[[Path], Dataset]] = {
    vehicle_list.NAME: vehicle_list.read,
    duk_register.NAME: duk_register.read,
    pid_vehicles.NAME: pid_vehicles.read,
}


def add_parsers(commands: Any) -> None:
    reference = commands.add_parser("ref", help="curated reference datasets")
    reference_commands = reference.add_subparsers(dest="ref_command", required=True)
    importer = reference_commands.add_parser("import", help="mirror a dataset into ref.*")
    importer.add_argument("dataset", choices=sorted(READERS))
    importer.add_argument("--path", type=Path, help="default: [reference] <dataset> in config")
    importer.add_argument("--config", type=Path)
    importer.add_argument("--database-url")
    fetcher = reference_commands.add_parser(
        "fetch", help="add to a dataset built from a source (slowly; resumes where it stopped)"
    )
    fetcher.add_argument("dataset", choices=[pid_vehicles.NAME])
    fetcher.add_argument("--path", type=Path, help="default: [reference] <dataset> in config")
    fetcher.add_argument("--config", type=Path)
    fetcher.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    fetcher.add_argument("--refresh-days", type=int, default=30)
    fetcher.add_argument("--limit", type=int, default=3000, help="requests at most")
    fetcher.add_argument(
        "--import",
        dest="load",
        action="store_true",
        help="import into ref.* every --import-every vehicles and at the end",
    )
    fetcher.add_argument("--import-every", type=int, default=200)
    fetcher.add_argument("--database-url")


def run(args: argparse.Namespace) -> int:
    if args.ref_command == "fetch":
        return _fetch(args)
    name = cast(str, args.dataset)
    config = cast(Path | None, args.config)
    try:
        path = cast(Path | None, args.path) or load_reference_path(name, config)
        if path is None:
            raise ConfigurationError(f"no file for {name}: give --path or [reference] {name}")
        _import(name, path, cast(str | None, args.database_url) or load_database_url(config))
    except (ConfigurationError, OSError, ValueError, psycopg.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _import(name: str, path: Path, url: str) -> None:
    dataset = READERS[name](path)
    with psycopg.connect(url, autocommit=True) as connection:
        loaded = load_dataset(connection, dataset)
        if loaded:  # the realtime worker reloads the registry
            connection.execute(f"NOTIFY {REGISTRY_CHANNEL}")
    state = "loaded" if loaded else "already loaded"
    print(f"{name} {dataset.version[:12]}: {state} ({dataset.rows} rows)")


def _fetch(args: argparse.Namespace) -> int:
    name = cast(str, args.dataset)
    config = cast(Path | None, args.config)
    try:
        path = cast(Path | None, args.path) or load_reference_path(name, config)
        if path is None:
            raise ConfigurationError(f"no file for {name}: give --path or [reference] {name}")
        key = load_secrets(["golemio"], config).get("golemio")
        if key is None:
            raise ConfigurationError("no Golemio key: set OBEHY_SECRET_GOLEMIO")
        url = None
        if args.load:
            url = cast(str | None, args.database_url) or load_database_url(config)

        def checkpoint() -> None:
            if url is not None:
                _import(name, path, url)

        pid_vehicles.fetch(
            path,
            key,
            delay_s=cast(float, args.delay),
            refresh_days=cast(int, args.refresh_days),
            limit=cast(int, args.limit),
            checkpoint_every=cast(int, args.import_every),
            checkpoint=checkpoint,
        )
        checkpoint()
    except (ConfigurationError, OSError, ValueError, psycopg.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
