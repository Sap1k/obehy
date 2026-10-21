"""``obehy ref`` subcommands: import curated reference datasets into `ref.*`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psycopg

from obehy.reference import duk_register, vehicle_list
from obehy.reference.dataset import Dataset
from obehy.reference.load import load_dataset
from obehy.reference.registry import REGISTRY_CHANNEL
from obehy.runtime_config import ConfigurationError, load_database_url, load_reference_path

# Dataset name -> its reader: our own vehicle list (`vehicles`, in jrunify-ext-geodata) and
# seeds. A file comes from `--path` or `[reference] <name>` in the config; a private one (the
# DÚK register) never from a repository.
READERS: dict[str, Callable[[Path], Dataset]] = {
    vehicle_list.NAME: vehicle_list.read,
    duk_register.NAME: duk_register.read,
}


def add_parsers(commands: Any) -> None:
    reference = commands.add_parser("ref", help="curated reference datasets")
    reference_commands = reference.add_subparsers(dest="ref_command", required=True)
    importer = reference_commands.add_parser("import", help="mirror a dataset into ref.*")
    importer.add_argument("dataset", choices=sorted(READERS))
    importer.add_argument("--path", type=Path, help="default: [reference] <dataset> in config")
    importer.add_argument("--config", type=Path)
    importer.add_argument("--database-url")


def run(args: argparse.Namespace) -> int:
    name = cast(str, args.dataset)
    config = cast(Path | None, args.config)
    try:
        path = cast(Path | None, args.path) or load_reference_path(name, config)
        if path is None:
            raise ConfigurationError(f"no file for {name}: give --path or [reference] {name}")
        dataset = READERS[name](path)
        url = cast(str | None, args.database_url) or load_database_url(config)
        with psycopg.connect(url, autocommit=True) as connection:
            loaded = load_dataset(connection, dataset)
            if loaded:  # the realtime worker reloads the registry
                connection.execute(f"NOTIFY {REGISTRY_CHANNEL}")
    except (ConfigurationError, OSError, ValueError, psycopg.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    state = "loaded" if loaded else "already loaded"
    print(f"{name} {dataset.version[:12]}: {state} ({dataset.rows} rows)")
    return 0
