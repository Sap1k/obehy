"""`obehy rt record`: poll realtime channels and archive their payloads without processing them.

The only exception is a channel with a named filter, which drops entries Oběhy never uses
before the payload is stored (Arriva's fleet-wide feed is reduced to Arriva Express).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import signal
import statistics
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.manifest import SOURCES, Channel, ManifestError, select_channels
from obehy.realtime.manifest import load_channels as _load_channels
from obehy.realtime.runtime.fetch import fetch
from obehy.realtime.runtime.scheduler import backoff_interval, run_channel

MANIFEST = SOURCES
SUMMARY_INTERVAL_S = 600.0


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


def load_channels(path: Path = SOURCES) -> list[Channel]:
    """Connector channels with the recorder's payload filters known."""

    return _load_channels(path, tuple(FILTERS))


__all__ = [
    "Channel",
    "ManifestError",
    "backoff_interval",
    "fetch",
    "load_channels",
    "select_channels",
]


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


FetchFn = Callable[[Channel], Poll]


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


def log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}", flush=True)


def _on_poll(
    writer: ArchiveWriter, stats: dict[str, ChannelStats]
) -> Callable[[Channel, Poll], Awaitable[None]]:
    async def on_poll(channel: Channel, poll: Poll) -> None:
        archive_poll(writer, channel, poll, stats[channel.name])

    return on_poll


def archive_poll(
    writer: ArchiveWriter, channel: Channel, poll: Poll, stats: ChannelStats
) -> str | None:
    """Store a poll (filtered if the channel says so); return the stored payload's sha256."""

    body, extra = archived_payload(channel, poll)
    stored = writer.append(channel.source, channel.channel, poll, body, extra)
    stats.polls += 1
    stats.latencies_ms.append((poll.received_at - poll.requested_at) / timedelta(milliseconds=1))
    if stored.new_object_bytes:
        stats.new_objects += 1
        stats.stored_bytes += stored.new_object_bytes
    if "filter_error" in extra:
        stats.filter_errors += 1
        log(f"{channel.name}: filter error, stored unfiltered: {extra['filter_error']}")
    if not poll.ok:
        stats.errors += 1
        log(f"{channel.name}: {poll.error or f'HTTP {poll.status}'}")
    return stored.sha256


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
            log(channel_stats.line(name))
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
    log(f"recording {', '.join(channel.name for channel in channels)} into {archive}")
    try:
        on_poll = _on_poll(writer, stats)
        await asyncio.gather(
            *(run_channel(channel, stop, fetcher, on_poll, once=once) for channel in channels)
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
