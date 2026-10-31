"""Connector manifests: `realtime/sources/<source>.toml` (docs/R1_SLICE.md section 4).

A connector declares how its channels are polled and how its lookups are asked (BASE_PLAN.md
section 18.2); the generic runtime (recorder, worker, lookup broker) executes that.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast

from obehy.realtime.model import SourceSemantics

SOURCES = Path(__file__).resolve().parent / "sources"
FEED_NAMES = ("jdf", "czptt")
POLL_KINDS = ("interval", "demand")
LOOKUP_KINDS = ("composition",)
DEMAND_QUERIES = ("station_boards",)
CAPABILITIES = (
    "vehicle_key",
    "trip_key",
    "line_ref",
    "destination",
    "position",
    "delay",
    "source_state",
    "stop_event",
    "next_stop",
    "platform",
    "alert",
    "composition",
)


class ManifestError(ValueError):
    """A connector manifest is invalid."""


@dataclass(frozen=True)
class Auth:
    """A request header whose value is a named secret (`runtime_config.load_secrets`); a
    channel whose secret is not configured never polls."""

    header: str
    secret: str


@dataclass(frozen=True)
class Cursor:
    """Delta polling: the response header of the last good poll goes back as a query
    parameter, so the source returns only what changed since. Without one, or when the last
    good poll is older than `max_age_s`, the poll is full."""

    header: str
    param: str
    max_age_s: float


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
    # The feeds whose observations the worker processes live; None (no `live_feeds` in the
    # manifest) is every feed. Replay always decodes every feed, so a connector is checked on
    # recorded days before it goes live.
    live_feeds: tuple[str, ...] | None = None
    capabilities: tuple[str, ...] = ()
    semantics: dict[str, Any] = field(default_factory=dict[str, Any])
    # Sent only through the proxy pool, anonymously (runtime/egress.py); never directly.
    proxied: bool = False
    # Demand polling: the named query that says what to request (runtime/demand.py); the
    # request body's `{sr70}` is filled per read. Interval channels have none.
    demand: str | None = None
    auth: Auth | None = None
    cursor: Cursor | None = None

    def is_live(self, feed: str) -> bool:
        return self.live_feeds is None or feed in self.live_feeds

    @property
    def name(self) -> str:
        return f"{self.source}/{self.channel}"

    @property
    def core_semantics(self) -> SourceSemantics:
        return _core_semantics(self.semantics, self.name)


@dataclass(frozen=True)
class Rate:
    """A token bucket: `per_minute` requests on average, at most `burst` at once."""

    per_minute: float
    burst: int


@dataclass(frozen=True)
class Applies:
    """Which trips a lookup answers for, declared so that the broker and the API agree without
    asking upstream: a trip key per namespace matching a pattern (all of them), and service
    dates within `days_around` of today. Empty: every trip of the lookup's feeds, any day."""

    keys: tuple[tuple[str, str], ...] = ()  # (namespace, regex searched in the key)
    days_around: int | None = None

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(namespace for namespace, _ in self.keys)

    def refusal(self, keys: Mapping[str, str], service_date: date, today: date) -> str | None:
        """Why the lookup does not answer for a trip with these keys on the date; None if it
        does."""

        for namespace, pattern in self.keys:
            key = keys.get(namespace)
            if key is None or re.search(pattern, key) is None:
                return f"not a trip it answers for ({namespace})"
        if self.days_around is not None and abs((service_date - today).days) > self.days_around:
            return f"answers only within {self.days_around} day(s) of today"
        return None


@dataclass(frozen=True)
class Lookup:
    """A lazy detail (a composition): asked for on demand, never polled. `request` is the
    upstream call as a channel (method, URL whose `{name}` placeholders the connector's plan
    fills, headers, timeout, proxied), so it is fetched and archived like a poll."""

    source: str
    name: str
    kind: str
    feeds: tuple[str, ...]
    request: Channel
    ttl_s: float  # an answer is reused this long
    not_found_ttl_s: float  # "no such train" is believed this long
    retry_s: float  # a failed request is not repeated sooner
    rate: Rate
    daily_budget: int  # requests per day (Europe/Prague)
    applies: Applies = Applies()

    @property
    def qualified(self) -> str:
        return f"{self.source}/{self.name}"


@dataclass(frozen=True)
class Source:
    """One connector manifest: its channels and lookups, and how the public feed list names
    it."""

    source: str
    title: str  # e.g. "DÚK vehicle positions (Ústecký kraj)"
    covers: str  # what it covers and contributes, one or two sentences
    channels: tuple[Channel, ...]
    lookups: tuple[Lookup, ...] = ()
    # Whether its vehicle IDs name physical vehicles, by mode ("default" for the rest): a train
    # or a metro run keyed by its train or trip has no vehicle to follow through a day (oběhy).
    physical_vehicles: dict[str, bool] = field(default_factory=lambda: {"default": True})

    def physical(self, mode: str | None) -> bool:
        """Whether its vehicle IDs on `mode`'s journeys (None: unknown) are physical vehicles."""

        found = self.physical_vehicles.get(mode) if mode is not None else None
        return found if found is not None else self.physical_vehicles.get("default", True)

    @property
    def live_feeds(self) -> tuple[str, ...]:
        """The feeds any of its channels feeds live, in `FEED_NAMES` order."""

        return tuple(
            feed
            for feed in FEED_NAMES
            if any(feed in channel.feeds and channel.is_live(feed) for channel in self.channels)
        )


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
    demand = poll.get("query") if poll.get("kind") == "demand" else None
    if poll.get("kind") == "demand" and demand not in DEMAND_QUERIES:
        raise ManifestError(f"{where}: poll.query must be one of {DEMAND_QUERIES}")
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
    feeds = _strings(table, "feeds", where, FEED_NAMES)
    live_feeds = _strings(table, "live_feeds", where, feeds) if "live_feeds" in table else None
    proxied = _proxied(table, where)
    auth: Auth | None = None
    if "auth" in table:
        auth_table = _table(table, "auth", where)
        auth = Auth(_string(auth_table, "header", where), _string(auth_table, "secret", where))
        if not _IDENTIFIER.match(auth.secret):
            raise ManifestError(f"{where}: auth.secret must be a lowercase ASCII name")
    cursor: Cursor | None = None
    if "cursor" in table:
        if method != "GET" or demand is not None:
            raise ManifestError(f"{where}: only interval GET channels take a cursor")
        cursor_table = _table(table, "cursor", where)
        cursor = Cursor(
            _string(cursor_table, "header", where).lower(),
            _string(cursor_table, "param", where),
            _positive(cursor_table, "max_age_s", where),
        )
    return Channel(
        source=source,
        channel=name,
        method=method,
        url=_string(request, "url", where),
        interval_s=60.0 if demand is not None else _positive(poll, "seconds", where),
        timeout_s=_positive(table, "timeout_s", where),
        headers=dict(cast(dict[str, str], headers)),
        body=None if body is None else body.encode("utf-8"),
        filter=cast(str | None, filter_name),
        backoff_after=int(_positive(backoff, "after_failures", where)),
        max_backoff_s=_positive(backoff, "max_s", where),
        feeds=feeds,
        live_feeds=live_feeds,
        capabilities=_strings(table, "capabilities", where, CAPABILITIES),
        semantics=dict(cast(dict[str, Any], semantics)),
        proxied=proxied,
        demand=demand,
        auth=auth,
        cursor=cursor,
    )


def _proxied(table: dict[str, Any], where: str) -> bool:
    if "egress" in table:
        raise ManifestError(f"{where}: egress was replaced by proxied = true (one proxy pool)")
    proxied = table.get("proxied", False)
    if not isinstance(proxied, bool):
        raise ManifestError(f"{where}: proxied must be true or false")
    return proxied


def _lookup(source: str, raw: object, where: str) -> Lookup:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: not a table")
    table = cast(dict[str, Any], raw)
    name = _string(table, "name", where)
    if not _IDENTIFIER.match(name):
        raise ManifestError(f"{where}: {name!r} must be lowercase ASCII and dashes")
    kind = table.get("kind")
    if kind not in LOOKUP_KINDS:
        raise ManifestError(f"{where}: kind must be one of {LOOKUP_KINDS}")
    request = _table(table, "request", where)
    if request.get("method") != "GET":
        raise ManifestError(f"{where}: a lookup's request.method must be GET")
    headers = request.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(value, str) for value in cast(dict[str, object], headers).values()
    ):
        raise ManifestError(f"{where}: request.headers must be a table of strings")
    proxied = _proxied(table, where)
    applies = _applies(table, where)
    rate = _table(table, "rate", where)
    feeds = _strings(table, "feeds", where, FEED_NAMES)
    channel = Channel(
        source=source,
        channel=name,
        method="GET",
        url=_string(request, "url", where),
        interval_s=0.0,  # never polled
        timeout_s=_positive(table, "timeout_s", where),
        headers=dict(cast(dict[str, str], headers)),
        feeds=feeds,
        capabilities=_strings(table, "capabilities", where, CAPABILITIES),
        proxied=proxied,
    )
    return Lookup(
        source=source,
        name=name,
        kind=str(kind),
        feeds=feeds,
        request=channel,
        ttl_s=_positive(table, "ttl_s", where),
        not_found_ttl_s=_positive(table, "not_found_ttl_s", where),
        retry_s=_positive(table, "retry_s", where),
        rate=Rate(_positive(rate, "per_minute", where), int(_positive(rate, "burst", where))),
        daily_budget=int(_positive(table, "daily_budget", where)),
        applies=applies,
    )


def _applies(table: dict[str, Any], where: str) -> Applies:
    if "applies" not in table:
        return Applies()
    applies = _table(table, "applies", where)
    keys = applies.get("keys", {})
    if not isinstance(keys, dict):
        raise ManifestError(f"{where}: applies.keys must be a table of patterns")
    patterns: list[tuple[str, str]] = []
    for namespace, pattern in sorted(cast(dict[str, object], keys).items()):
        if not isinstance(pattern, str):
            raise ManifestError(f"{where}: applies.keys.{namespace} must be a string")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ManifestError(f"{where}: applies.keys.{namespace}: {error}") from error
        patterns.append((namespace, pattern))
    days = applies.get("days_around")
    if days is not None and (not isinstance(days, int) or isinstance(days, bool) or days < 0):
        raise ManifestError(f"{where}: applies.days_around must be a whole number of days")
    return Applies(tuple(patterns), days)


def load_source(path: Path, filters: Sequence[str] = ()) -> Source:
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
    tables = document.get("channel", [])
    lookup_tables = document.get("lookup", [])
    if not isinstance(tables, list) or not isinstance(lookup_tables, list):
        raise ManifestError(f"{path}: [[channel]] and [[lookup]] must be arrays of tables")
    if not tables and not lookup_tables:
        raise ManifestError(f"{path} defines no [[channel]] and no [[lookup]]")
    channels: list[Channel] = []
    for number, raw in enumerate(cast(list[object], tables), start=1):
        channel = _channel(source, raw, f"{path} channel {number}", filters)
        if any(existing.name == channel.name for existing in channels):
            raise ManifestError(f"{path}: duplicate channel {channel.name}")
        channels.append(channel)
    lookups: list[Lookup] = []
    for number, raw in enumerate(cast(list[object], lookup_tables), start=1):
        lookup = _lookup(source, raw, f"{path} lookup {number}")
        taken = {c.channel for c in channels} | {existing.name for existing in lookups}
        if lookup.name in taken:
            raise ManifestError(f"{path}: duplicate channel or lookup {lookup.name}")
        lookups.append(lookup)
    return Source(
        source=source,
        title=_string(document, "title", str(path)),
        covers=_string(document, "covers", str(path)),
        channels=tuple(channels),
        lookups=tuple(lookups),
        physical_vehicles=_physical_vehicles(document, str(path)),
    )


def _physical_vehicles(document: dict[str, Any], where: str) -> dict[str, bool]:
    vehicles = document.get("vehicles", {})
    if not isinstance(vehicles, dict):
        raise ManifestError(f"{where}: [vehicles] must be a table")
    raw = cast(dict[str, Any], vehicles).get("physical", {})
    if not isinstance(raw, dict) or any(
        not isinstance(v, bool) for v in cast(dict[str, object], raw).values()
    ):
        raise ManifestError(f"{where}: vehicles.physical must map modes to true or false")
    return {"default": True, **cast(dict[str, bool], raw)}


def load_manifest(path: Path, filters: Sequence[str] = ()) -> list[Channel]:
    return list(load_source(path, filters).channels)


def _manifest_files(path: Path) -> list[Path]:
    files = sorted(path.glob("*.toml")) if path.is_dir() else [path]
    if not files:
        raise ManifestError(f"no connector manifests in {path}")
    return files


def load_sources(path: Path = SOURCES, filters: Sequence[str] = ()) -> list[Source]:
    """Every connector manifest in a directory (by file name), or one manifest file."""

    return [load_source(file, filters) for file in _manifest_files(path)]


def physical_vehicles(path: Path = SOURCES) -> dict[str, Source]:
    """Each source's `[vehicles]` only, for readers that run no connector (the API): the rest
    of a returned `Source` is empty."""

    found: dict[str, Source] = {}
    for file in _manifest_files(path):
        with file.open("rb") as stream:
            document = tomllib.load(stream)
        source = _string(document, "source", str(file))
        found[source] = Source(
            source=source,
            title="",
            covers="",
            channels=(),
            physical_vehicles=_physical_vehicles(document, str(file)),
        )
    return found


def load_channels(path: Path = SOURCES, filters: Sequence[str] = ()) -> list[Channel]:
    """Every channel of a manifest file, or of every `*.toml` in a directory (by file name)."""

    return [channel for source in load_sources(path, filters) for channel in source.channels]


def declared_lookups(path: Path = SOURCES) -> list[Lookup]:
    """Every `[[lookup]]` only, for readers that run no connector (the API): which lookups
    there are and which trips they answer for."""

    found: list[Lookup] = []
    for file in _manifest_files(path):
        with file.open("rb") as stream:
            document = tomllib.load(stream)
        source = _string(document, "source", str(file))
        tables = cast(list[object], document.get("lookup", []))
        found.extend(
            _lookup(source, raw, f"{file} lookup {n}") for n, raw in enumerate(tables, start=1)
        )
    return found


def load_lookups(path: Path = SOURCES, filters: Sequence[str] = ()) -> list[Lookup]:
    """Every lookup of a manifest file, or of every `*.toml` in a directory (by file name)."""

    return [lookup for source in load_sources(path, filters) for lookup in source.lookups]


def select_channels(channels: Sequence[Channel], sources: Sequence[str] | None) -> list[Channel]:
    if not sources:
        return list(channels)
    known = {channel.source for channel in channels}
    unknown = sorted(set(sources) - known)
    if unknown:
        raise ManifestError(f"Unknown source(s) {', '.join(unknown)}; known: {sorted(known)}")
    return [channel for channel in channels if channel.source in sources]
