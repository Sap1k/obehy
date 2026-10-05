"""`obehy rt record`: poll realtime channels and archive their payloads without processing them.

The only exception is a channel with a named filter, which drops entries Oběhy never uses
before the payload is stored (Arriva's fleet-wide feed is reduced to Arriva Express).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
import signal
import statistics
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.request import urlopen

from obehy.pipeline.download import http_request
from obehy.realtime.archive import ArchiveWriter, Poll

MANIFEST = Path(__file__).resolve().parents[1] / "data" / "realtime" / "sources.toml"
KEPT_HEADERS = ("age", "date", "etag", "last-modified")
BACKOFF_AFTER = 5
MAX_BACKOFF_S = 300.0
SUMMARY_INTERVAL_S = 600.0


class ManifestError(ValueError):
    """The realtime channel manifest is invalid."""


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

    @property
    def name(self) -> str:
        return f"{self.source}/{self.channel}"


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _string(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: {key!r} must be a non-empty string")
    return value


def _seconds(table: dict[str, Any], key: str, where: str) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ManifestError(f"{where}: {key!r} must be a positive number")
    return float(value)


def load_channels(path: Path = MANIFEST) -> list[Channel]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(f"Invalid TOML in {path}: {error}") from error
    if document.get("schema_version") != 1:
        raise ManifestError(f"{path} must contain schema_version = 1")
    tables = document.get("channel")
    if not isinstance(tables, list) or not tables:
        raise ManifestError(f"{path} defines no [[channel]]")
    channels: list[Channel] = []
    for number, raw in enumerate(cast(list[object], tables), start=1):
        where = f"{path} channel {number}"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where}: not a table")
        table = cast(dict[str, Any], raw)
        source = _string(table, "source", where)
        name = _string(table, "channel", where)
        for value in (source, name):
            if not _IDENTIFIER.match(value):
                raise ManifestError(f"{where}: {value!r} must be lowercase ASCII and dashes")
        method = _string(table, "method", where)
        if method not in ("GET", "POST"):
            raise ManifestError(f"{where}: method must be GET or POST")
        headers = table.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(value, str) for value in cast(dict[str, object], headers).values()
        ):
            raise ManifestError(f"{where}: headers must be a table of strings")
        body = table.get("body")
        if body is not None and not isinstance(body, str):
            raise ManifestError(f"{where}: body must be a string")
        if body is not None and method != "POST":
            raise ManifestError(f"{where}: only POST channels take a body")
        filter_name = table.get("filter")
        if filter_name is not None and filter_name not in FILTERS:
            raise ManifestError(f"{where}: unknown filter {filter_name!r}")
        channel = Channel(
            source=source,
            channel=name,
            method=method,
            url=_string(table, "url", where),
            interval_s=_seconds(table, "interval_s", where),
            timeout_s=_seconds(table, "timeout_s", where),
            headers=dict(cast(dict[str, str], headers)),
            body=None if body is None else body.encode("utf-8"),
            filter=cast(str | None, filter_name),
        )
        if any(existing.name == channel.name for existing in channels):
            raise ManifestError(f"{where}: duplicate channel {channel.name}")
        channels.append(channel)
    return channels


def select_channels(channels: Sequence[Channel], sources: Sequence[str] | None) -> list[Channel]:
    if not sources:
        return list(channels)
    known = {channel.source for channel in channels}
    unknown = sorted(set(sources) - known)
    if unknown:
        raise ManifestError(f"Unknown source(s) {', '.join(unknown)}; known: {sorted(known)}")
    return [channel for channel in channels if channel.source in sources]


class FilterError(ValueError):
    """The payload does not have the shape the filter expects."""


@dataclass(frozen=True)
class Filtered:
    body: bytes
    kept: int
    dropped: int


@dataclass(frozen=True)
class PayloadFilter:
    version: int
    apply: Callable[[bytes], Filtered]

    def label(self, name: str) -> str:
        return f"{name}@{self.version}"


def _arriva_express(body: bytes) -> Filtered:
    """Keep only `mainType = "ARRIVA EXPRESS"` vehicles in the GraphQL batch response."""

    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FilterError(f"not JSON: {error}") from error
    if not isinstance(document, list):
        raise FilterError("expected a GraphQL batch (JSON array)")
    kept = dropped = 0
    for item in cast(list[object], document):
        data = cast(dict[str, Any], item).get("data") if isinstance(item, dict) else None
        if not isinstance(data, dict):
            raise FilterError("batch item without a data object")
        vehicles = cast(dict[str, Any], data).get("busesCurrentLocations")
        if not isinstance(vehicles, list):
            raise FilterError("data.busesCurrentLocations is not a list")
        entries = cast(list[object], vehicles)
        express: list[object] = [
            vehicle
            for vehicle in entries
            if isinstance(vehicle, dict)
            and cast(dict[str, Any], vehicle).get("mainType") == "ARRIVA EXPRESS"
        ]
        kept += len(express)
        dropped += len(entries) - len(express)
        cast(dict[str, Any], data)["busesCurrentLocations"] = express
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Filtered(encoded, kept, dropped)


FILTERS: dict[str, PayloadFilter] = {
    "arriva-express": PayloadFilter(version=1, apply=_arriva_express),
}


def archived_payload(channel: Channel, poll: Poll) -> tuple[bytes | None, dict[str, object]]:
    """The body to store and the extra index fields for a channel's filter."""

    if channel.filter is None:
        return poll.body, {}
    payload_filter = FILTERS[channel.filter]
    extra: dict[str, object] = {"filter": payload_filter.label(channel.filter)}
    if poll.body is None or not poll.ok:
        return poll.body, extra
    extra["source_bytes"] = len(poll.body)
    extra["source_sha256"] = hashlib.sha256(poll.body).hexdigest()
    try:
        filtered = payload_filter.apply(poll.body)
    except FilterError as error:
        # Store the unfiltered payload so that a schema change is never lost silently.
        extra["filter_error"] = str(error)
        return poll.body, extra
    extra["kept"] = filtered.kept
    extra["dropped"] = filtered.dropped
    return filtered.body, extra


class _Response(Protocol):
    status: int
    headers: Any

    def read(self) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


Clock = Callable[[], datetime]
FetchFn = Callable[[Channel], Poll]


def utc_clock() -> datetime:
    return datetime.now(UTC)


def _kept_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in KEPT_HEADERS:
        value = cast(str | None, headers.get(name))
        if value is not None:
            result[name] = value
    return result


def fetch(channel: Channel, clock: Clock = utc_clock) -> Poll:
    request = http_request(channel.url, data=channel.body, headers=channel.headers)
    request.method = channel.method
    requested_at = clock()
    try:
        with cast(_Response, urlopen(request, timeout=channel.timeout_s)) as response:
            body = response.read()
            return Poll(
                requested_at=requested_at,
                received_at=clock(),
                status=response.status,
                body=body,
                content_type=cast(str | None, response.headers.get("Content-Type")),
                headers=_kept_headers(response.headers),
            )
    except HTTPError as error:
        try:
            body = error.read()
        except OSError:
            body = None
        return Poll(
            requested_at=requested_at,
            received_at=clock(),
            status=error.code,
            body=body or None,
            content_type=error.headers.get("Content-Type") if error.headers else None,
            headers=_kept_headers(error.headers) if error.headers else {},
            error=f"HTTP {error.code} {error.reason}",
        )
    except (OSError, ValueError) as error:
        return Poll(
            requested_at=requested_at,
            received_at=clock(),
            status=None,
            body=None,
            error=f"{type(error).__name__}: {error}",
        )


@dataclass
class ChannelStats:
    polls: int = 0
    errors: int = 0
    filter_errors: int = 0
    new_objects: int = 0
    stored_bytes: int = 0
    latencies_ms: list[float] = field(default_factory=list[float])

    def line(self, name: str) -> str:
        latency = statistics.median(self.latencies_ms) if self.latencies_ms else math.nan
        text = (
            f"{name}: {self.polls} polls, {self.errors} errors, {self.new_objects} new payloads, "
            f"{self.stored_bytes:,} bytes stored, median {latency:.0f} ms"
        )
        if self.filter_errors:
            text += f", {self.filter_errors} filter errors"
        return text


def backoff_interval(channel: Channel, consecutive_failures: int) -> float:
    if consecutive_failures < BACKOFF_AFTER:
        return channel.interval_s
    doubled = channel.interval_s * 2 ** (consecutive_failures - BACKOFF_AFTER + 1)
    return min(MAX_BACKOFF_S, max(channel.interval_s, doubled))


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}", flush=True)


async def _record_channel(
    channel: Channel,
    writer: ArchiveWriter,
    stats: ChannelStats,
    stop: asyncio.Event,
    fetcher: FetchFn,
    once: bool,
) -> None:
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    failures = 0
    while not stop.is_set():
        poll = await asyncio.to_thread(fetcher, channel)
        body, extra = archived_payload(channel, poll)
        stored = writer.append(channel.source, channel.channel, poll, body, extra)
        stats.polls += 1
        stats.latencies_ms.append(
            (poll.received_at - poll.requested_at) / timedelta(milliseconds=1)
        )
        if stored.new_object_bytes:
            stats.new_objects += 1
            stats.stored_bytes += stored.new_object_bytes
        if "filter_error" in extra:
            stats.filter_errors += 1
            _log(f"{channel.name}: filter error, stored unfiltered: {extra['filter_error']}")
        if poll.ok:
            failures = 0
        else:
            stats.errors += 1
            failures += 1
            _log(f"{channel.name}: {poll.error or f'HTTP {poll.status}'}")
        if once:
            return
        interval = backoff_interval(channel, failures)
        now = loop.time()
        if interval != channel.interval_s:
            next_tick = now + interval
        else:
            next_tick += interval
            if next_tick <= now:
                # Skip missed ticks instead of bunching requests after a slow poll.
                next_tick += math.ceil((now - next_tick) / interval + 1e-9) * interval
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, next_tick - now))


async def record(
    channels: Sequence[Channel],
    archive: Path,
    *,
    once: bool = False,
    duration_s: float | None = None,
    fetcher: FetchFn = fetch,
    summary_interval_s: float = SUMMARY_INTERVAL_S,
) -> dict[str, ChannelStats]:
    writer = ArchiveWriter(archive)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    stats = {channel.name: ChannelStats() for channel in channels}

    def summarize() -> None:
        for name, channel_stats in stats.items():
            _log(channel_stats.line(name))
            channel_stats.latencies_ms.clear()

    async def summaries() -> None:
        while True:
            await asyncio.sleep(summary_interval_s)
            summarize()

    async def deadline(seconds: float) -> None:
        await asyncio.sleep(seconds)
        stop.set()

    helpers: list[asyncio.Task[None]] = []
    if not once:
        helpers.append(asyncio.create_task(summaries()))
        if duration_s is not None:
            helpers.append(asyncio.create_task(deadline(duration_s)))
    _log(f"recording {', '.join(channel.name for channel in channels)} into {archive}")
    try:
        await asyncio.gather(
            *(
                _record_channel(channel, writer, stats[channel.name], stop, fetcher, once)
                for channel in channels
            )
        )
    finally:
        for helper in helpers:
            helper.cancel()
        summarize()
    return stats


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    match = _DURATION.match(text.strip())
    if match is None:
        raise ValueError(f"invalid duration {text!r}; use e.g. 90s, 30m, 6h or 2d")
    return float(match.group(1)) * _UNITS[match.group(2)]
