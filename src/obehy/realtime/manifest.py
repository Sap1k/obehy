"""Connector manifests: `realtime/sources/<source>.toml` (docs/R1_SLICE.md section 4).

A connector declares how its channels are polled; the generic runtime (recorder, worker)
executes that. Lookups arrive with the first lookup connector.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from obehy.realtime.model import SourceSemantics

SOURCES = Path(__file__).resolve().parent / "sources"
FEED_NAMES = ("jdf", "czptt")
POLL_KINDS = ("interval",)
CAPABILITIES = (
    "vehicle_key",
    "trip_key",
    "position",
    "delay",
    "source_state",
    "stop_event",
    "next_stop",
)


class ManifestError(ValueError):
    """A connector manifest is invalid."""


@dataclass(frozen=True)
class Channel:
    source: str
    channel: str
    method: str
    url: str
    interval_s: float
    timeout_s: float
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: bytes | None = None
    filter: str | None = None
    backoff_after: int = 5
    max_backoff_s: float = 300.0
    feeds: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    semantics: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def name(self) -> str:
        return f"{self.source}/{self.channel}"

    @property
    def core_semantics(self) -> SourceSemantics:
        return _core_semantics(self.semantics, self.name)


CORE_SEMANTICS = tuple(SourceSemantics.__dataclass_fields__)


def _core_semantics(semantics: dict[str, Any], where: str) -> SourceSemantics:
    flags: dict[str, bool] = {}
    for name in CORE_SEMANTICS:
        value = semantics.get(name, False)
        if not isinstance(value, bool):
            raise ManifestError(f"{where}: semantics.{name} must be true or false")
        flags[name] = value
    return SourceSemantics(**flags)


def semantics_by_channel(channels: Sequence[Channel]) -> dict[tuple[str, str], SourceSemantics]:
    """The core semantics of each `(source, channel)`, as `Context.semantics` takes them."""

    return {(channel.source, channel.channel): channel.core_semantics for channel in channels}


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _table(table: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = table.get(key)
    if not isinstance(value, dict):
        raise ManifestError(f"{where}: {key!r} must be a table")
    return cast(dict[str, Any], value)


def _string(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: {key!r} must be a non-empty string")
    return value


def _positive(table: dict[str, Any], key: str, where: str) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ManifestError(f"{where}: {key!r} must be a positive number")
    return float(value)


def _strings(
    table: dict[str, Any], key: str, where: str, allowed: Sequence[str]
) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(v, str) for v in cast(list[object], value)
    ):
        raise ManifestError(f"{where}: {key!r} must be a list of strings")
    unknown = sorted(set(cast(list[str], value)) - set(allowed))
    if unknown:
        raise ManifestError(f"{where}: unknown {key} {unknown}")
    return tuple(cast(list[str], value))


def _channel(source: str, raw: object, where: str, filters: Sequence[str]) -> Channel:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: not a table")
    table = cast(dict[str, Any], raw)
    name = _string(table, "name", where)
    if not _IDENTIFIER.match(name):
        raise ManifestError(f"{where}: {name!r} must be lowercase ASCII and dashes")
    poll = _table(table, "poll", where)
    if poll.get("kind") not in POLL_KINDS:
        raise ManifestError(f"{where}: poll.kind must be one of {POLL_KINDS}")
    backoff = _table(table, "backoff", where)
    request = _table(table, "request", where)
    method = _string(request, "method", where)
    if method not in ("GET", "POST"):
        raise ManifestError(f"{where}: request.method must be GET or POST")
    headers = request.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(value, str) for value in cast(dict[str, object], headers).values()
    ):
        raise ManifestError(f"{where}: request.headers must be a table of strings")
    body = request.get("body")
    if body is not None and not isinstance(body, str):
        raise ManifestError(f"{where}: request.body must be a string")
    if body is not None and method != "POST":
        raise ManifestError(f"{where}: only POST requests take a body")
    filter_name = table.get("filter")
    if filter_name is not None and filter_name not in filters:
        raise ManifestError(f"{where}: unknown filter {filter_name!r}")
    semantics = table.get("semantics", {})
    if not isinstance(semantics, dict):
        raise ManifestError(f"{where}: semantics must be a table")
    _core_semantics(cast(dict[str, Any], semantics), where)
    return Channel(
        source=source,
        channel=name,
        method=method,
        url=_string(request, "url", where),
        interval_s=_positive(poll, "seconds", where),
        timeout_s=_positive(table, "timeout_s", where),
        headers=dict(cast(dict[str, str], headers)),
        body=None if body is None else body.encode("utf-8"),
        filter=cast(str | None, filter_name),
        backoff_after=int(_positive(backoff, "after_failures", where)),
        max_backoff_s=_positive(backoff, "max_s", where),
        feeds=_strings(table, "feeds", where, FEED_NAMES),
        capabilities=_strings(table, "capabilities", where, CAPABILITIES),
        semantics=dict(cast(dict[str, Any], semantics)),
    )


def load_manifest(path: Path, filters: Sequence[str] = ()) -> list[Channel]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(f"Invalid TOML in {path}: {error}") from error
    if document.get("manifest_version") != 1:
        raise ManifestError(f"{path} must contain manifest_version = 1")
    source = _string(document, "source", str(path))
    if not _IDENTIFIER.match(source) or path.stem != source:
        raise ManifestError(f"{path}: source {source!r} must match the file name")
    tables = document.get("channel")
    if not isinstance(tables, list) or not tables:
        raise ManifestError(f"{path} defines no [[channel]]")
    channels: list[Channel] = []
    for number, raw in enumerate(cast(list[object], tables), start=1):
        channel = _channel(source, raw, f"{path} channel {number}", filters)
        if any(existing.name == channel.name for existing in channels):
            raise ManifestError(f"{path}: duplicate channel {channel.name}")
        channels.append(channel)
    return channels


def load_channels(path: Path = SOURCES, filters: Sequence[str] = ()) -> list[Channel]:
    """Every channel of a manifest file, or of every `*.toml` in a directory (by file name)."""

    files = sorted(path.glob("*.toml")) if path.is_dir() else [path]
    if not files:
        raise ManifestError(f"no connector manifests in {path}")
    return [channel for file in files for channel in load_manifest(file, filters)]


def select_channels(channels: Sequence[Channel], sources: Sequence[str] | None) -> list[Channel]:
    if not sources:
        return list(channels)
    known = {channel.source for channel in channels}
    unknown = sorted(set(sources) - known)
    if unknown:
        raise ManifestError(f"Unknown source(s) {', '.join(unknown)}; known: {sorted(known)}")
    return [channel for channel in channels if channel.source in sources]
