"""Machine-local configuration shared by the national builders."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable
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
    artifact_root: Path
    osm_file: Path
    # Routed post-inference evidence reused across runs; entries are
    # revalidated against the routing graph, so no pruning is needed.
    routing_cache_dir: Path
    # Published CZPTT source objects reused across runs; they are never rewritten upstream.
    czptt_source_cache_dir: Path
    jrunify_ext_geodata_dir: Path
    jrutil: JrUtilRuntime
    # The route-shapes companion binary (BASE_PLAN.md section 17); None skips route shapes.
    route_shapes_companion: Path | None = None
    # Routing PBF, osr graph and companion results reused across runs.
    route_shapes_cache_dir: Path | None = None


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


DATABASE_URL_ENV = "OBEHY_DATABASE_URL"
TEST_DATABASE_URL_ENV = "OBEHY_TEST_DATABASE_URL"


def load_database_url(path: Path | None = None) -> str:
    """The PostgreSQL URL: ``OBEHY_DATABASE_URL`` or ``[database] url`` in the config.

    Only ``schema_version`` and ``[database]`` are read, so a server without build paths can
    use the same file.
    """

    environment = os.environ.get(DATABASE_URL_ENV, "").strip()
    if environment:
        return environment
    source = (path or default_config_path()).resolve()
    if not source.is_file():
        raise ConfigurationError(
            f"No database configured: set {DATABASE_URL_ENV} or [database] url in {source}"
        )
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {source}: {error}") from error
    if document.get("schema_version") != 1:
        raise ConfigurationError(f"{source} must contain schema_version = 1")
    url = _table(document, "database").get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigurationError(f"Missing non-empty 'url' in [database] of {source}")
    return os.path.expandvars(url.strip())


def load_test_database_url(path: Path | None = None) -> str | None:
    """The DB-test admin URL: ``OBEHY_TEST_DATABASE_URL`` or ``[database] test_url``, if any."""

    environment = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if environment:
        return environment
    source = (path or default_config_path()).resolve()
    if not source.is_file():
        return None
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {source}: {error}") from error
    database = document.get("database")
    url = cast(dict[str, Any], database).get("test_url") if isinstance(database, dict) else None
    if not isinstance(url, str) or not url.strip():
        return None
    return os.path.expandvars(url.strip())


def secret_env(name: str) -> str:
    return f"OBEHY_SECRET_{name.upper().replace('-', '_')}"


def load_secrets(names: Iterable[str], path: Path | None = None) -> dict[str, str]:
    """The configured secrets among `names` (API keys of realtime sources): ``OBEHY_SECRET_<NAME>``
    or ``[realtime.secrets] <name>`` in the gitignored local config. Values are never logged."""

    wanted = sorted(set(names))
    found = {
        name: value for name in wanted if (value := os.environ.get(secret_env(name), "").strip())
    }
    source = (path or default_config_path()).resolve()
    if len(found) == len(wanted) or not source.is_file():
        return found
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {source}: {error}") from error
    realtime = document.get("realtime")
    table = cast(dict[str, Any], realtime).get("secrets") if isinstance(realtime, dict) else None
    if isinstance(table, dict):
        for name in wanted:
            value = cast(dict[str, Any], table).get(name)
            if name not in found and isinstance(value, str) and value.strip():
                found[name] = value.strip()
    return found


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

    workdir = _absolute_path(paths, "workdir", source)
    artifact_root = (
        _absolute_path(paths, "artifact_root", source)
        if paths.get("artifact_root") is not None
        else workdir
    )
    routing_cache_dir = (
        _absolute_path(paths, "routing_cache_dir", source)
        if paths.get("routing_cache_dir") is not None
        else workdir / "cache" / "routing"
    )
    czptt_source_cache_dir = (
        _absolute_path(paths, "czptt_source_cache_dir", source)
        if paths.get("czptt_source_cache_dir") is not None
        else workdir / "cache" / "czptt-sources"
    )
    return RuntimeConfig(
        source=source,
        workdir=workdir,
        artifact_root=artifact_root,
        routing_cache_dir=routing_cache_dir,
        czptt_source_cache_dir=czptt_source_cache_dir,
        osm_file=_absolute_path(paths, "osm_file", source),
        jrunify_ext_geodata_dir=_absolute_path(paths, "jrunify_ext_geodata_dir", source),
        jrutil=JrUtilRuntime(directory=directory, command=command),
        route_shapes_companion=(
            _absolute_path(paths, "route_shapes_companion", source)
            if paths.get("route_shapes_companion") is not None
            else None
        ),
        route_shapes_cache_dir=(
            _absolute_path(paths, "route_shapes_cache_dir", source)
            if paths.get("route_shapes_cache_dir") is not None
            else workdir / "cache" / "route-shapes"
        ),
    )


def load_reference_path(name: str, path: Path | None = None) -> Path | None:
    """A reference dataset's file: ``OBEHY_REFERENCE_<NAME>`` or ``[reference] <name>`` in the
    config. Private datasets (the DÚK register) live only there, never in the repository."""

    environment = os.environ.get(f"OBEHY_REFERENCE_{name.upper().replace('-', '_')}", "").strip()
    if environment:
        return Path(environment)
    source = (path or default_config_path()).resolve()
    if not source.is_file():
        return None
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {source}: {error}") from error
    reference = document.get("reference")
    value = cast(dict[str, Any], reference).get(name) if isinstance(reference, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(os.path.expandvars(value.strip()))
    return candidate if candidate.is_absolute() else (source.parent / candidate).resolve()
