"""Machine-local configuration shared by the national builders."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


class ConfigurationError(RuntimeError):
    """The machine-local Oběhy configuration is missing or invalid."""


@dataclass(frozen=True)
class JrUtilRuntime:
    directory: Path | None
    command: tuple[str, ...] | None


@dataclass(frozen=True)
class RuntimeConfig:
    source: Path
    workdir: Path
    osm_file: Path
    jrunify_ext_geodata_dir: Path
    jrutil: JrUtilRuntime


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "obehy.local.toml"


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigurationError(f"Missing [{name}] table")
    return cast(dict[str, Any], value)


def _absolute_path(table: dict[str, Any], key: str, source: Path) -> Path:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Missing non-empty {key!r} in {source}")
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{key!r} must be an absolute path in {source}: {path}")
    return path


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    source = (path or default_config_path()).resolve()
    if not source.is_file():
        raise ConfigurationError(
            f"Oběhy configuration does not exist: {source}. "
            "Copy config/obehy.example.toml to config/obehy.local.toml and edit it."
        )
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {source}: {error}") from error

    if document.get("schema_version") != 1:
        raise ConfigurationError(f"{source} must contain schema_version = 1")
    paths = _table(document, "paths")
    jrutil_table = _table(document, "jrutil")
    directory_value = jrutil_table.get("directory")
    command_value = jrutil_table.get("command")
    if (directory_value is None) == (command_value is None):
        raise ConfigurationError(
            f"{source} must set exactly one of jrutil.directory or jrutil.command"
        )

    directory: Path | None = None
    command: tuple[str, ...] | None = None
    if directory_value is not None:
        directory = _absolute_path(jrutil_table, "directory", source)
    else:
        command_parts = cast(list[object], command_value) if isinstance(command_value, list) else []
        if (
            not isinstance(command_value, list)
            or not command_parts
            or any(not isinstance(value, str) or not value for value in command_parts)
        ):
            raise ConfigurationError(
                f"jrutil.command in {source} must be a non-empty array of strings"
            )
        command = tuple(cast(list[str], command_parts))

    return RuntimeConfig(
        source=source,
        workdir=_absolute_path(paths, "workdir", source),
        osm_file=_absolute_path(paths, "osm_file", source),
        jrunify_ext_geodata_dir=_absolute_path(paths, "jrunify_ext_geodata_dir", source),
        jrutil=JrUtilRuntime(directory=directory, command=command),
    )
