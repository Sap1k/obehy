"""What the API, the release side, the jobs and the replay diagnostics read of the realtime
config, without the realtime worker (docs/RUST_PORT.md, "What runs where").

- Prague time at the edge: `PRAGUE`, `Instant`, `instant`, `ServiceTime`, `local_date` and the
  UTC text of the output stream. This is the only Python module that converts times.
- A few policy values (`policy-vNN.toml`): the journey namespaces, the vehicle-day gap, the
  retention windows, the disk guard and the keyless episode gap the replay report reads.
- From the connector manifests (`data/realtime/sources/*.toml`): the sources' descriptions and
  live feeds (the public feed list) and the declared lookups (which trips the API offers a lazy
  detail for).

Only those fields are read, and only they are checked; the rest of both schemas is the Rust
worker's (`obehy-core::config`), which reads the same files.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, NewType, cast
from zoneinfo import ZoneInfo

_PACKAGE = Path(__file__).resolve().parent
DATA = _PACKAGE / "data" / "realtime"
POLICY = DATA / "policy-v19.toml"
SOURCES = DATA / "sources"

FEED_NAMES = ("jdf", "czptt")
PREDICTORS = ("anchor_change", "sz", "propagate")  # policy `[rail] predictor`


class ConfigError(ValueError):
    """A realtime config file lacks a value read here, or has one of the wrong type."""


# --- Time at the edge --------------------------------------------------------------------------

PRAGUE = ZoneInfo("Europe/Prague")

Instant = NewType("Instant", datetime)


class TimeError(ValueError):
    """A value that cannot be a valid time in its declared meaning."""


def instant(value: datetime) -> Instant:
    """An aware datetime as a UTC `Instant`; naive input is a programming error."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise TimeError(f"naive datetime {value.isoformat()} cannot be an Instant")
    return Instant(value.astimezone(UTC))


@dataclass(frozen=True, slots=True, order=True)
class ServiceTime:
    """A schedule time: wall-clock seconds from local midnight of the service date, as JrUtil
    writes it (past 86400 it continues into the next day)."""

    service_date: date
    seconds: int

    def instant(self) -> Instant:
        """The instant of this wall-clock schedule time (first occurrence when repeated)."""

        return _schedule_instant(self.service_date, self.seconds)


@lru_cache(maxsize=1 << 16)  # schedule times repeat on every fix of a journey; bounded
def _schedule_instant(service_date: date, seconds: int) -> Instant:
    wall = datetime.combine(service_date, time()) + timedelta(seconds=seconds)
    return instant(wall.replace(tzinfo=PRAGUE, fold=0))


_EPOCH = Instant(datetime(1970, 1, 1, tzinfo=UTC))
_MICROSECOND = timedelta(microseconds=1)
_HOUR_US = 3_600_000_000


def micros(value: Instant) -> int:
    """Exact microseconds since the epoch, for arithmetic in the tracker's inner loop: a
    difference of two divided by 1_000_000 is bit-identical to `timedelta.total_seconds()`."""

    return (value - _EPOCH) // _MICROSECOND


def local_date(value: Instant) -> date:
    """The Europe/Prague calendar date of an instant."""

    return _local_date(micros(value) // _HOUR_US)


# Prague's UTC offsets are whole hours, so the local date is the same for a whole UTC hour:
# the conversion runs several times per observation and is cached per hour (bounded).
@lru_cache(maxsize=1 << 12)
def _local_date(hour: int) -> date:
    return datetime.fromtimestamp(hour * 3600, UTC).astimezone(PRAGUE).date()


def utc_text(value: datetime) -> str:
    """An aware time as UTC ISO 8601 with `Z`: `YYYY-MM-DDTHH:MM:SSZ`, with six digits of
    fractional seconds (`.ffffffZ`) only when the microseconds are not zero."""

    at = instant(value)
    text = (
        f"{at.year:04d}-{at.month:02d}-{at.day:02d}T{at.hour:02d}:{at.minute:02d}:{at.second:02d}"
    )
    if at.microsecond:
        text += f".{at.microsecond:06d}"
    return text + "Z"


def parse_utc(text: str) -> Instant:
    """An instant from `utc_text` (or any ISO 8601 time with an offset)."""

    return instant(datetime.fromisoformat(text))


# --- Reading TOML --------------------------------------------------------------------------------


def _document(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {path}: {error}") from error


def _table(data: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{where}{key} must be a table")
    return cast(dict[str, Any], value)


def _integer(data: Mapping[str, Any], key: str, where: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}{key} must be an integer")
    return value


def _number(data: Mapping[str, Any], key: str, where: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}{key} must be a number")
    return float(value)


def _text(data: Mapping[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}{key} must be a non-empty string")
    return value


def _names(
    data: Mapping[str, Any], key: str, where: str, allowed: tuple[str, ...]
) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item not in allowed for item in cast(list[object], value)
    ):
        raise ConfigError(f"{where}{key} must be a list of {allowed}")
    return tuple(cast(list[str], value))


# --- Policy values -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Retention:
    """How long stored realtime data is kept (`obehy jobs hourly` and `nightly`)."""

    observation_hours: int  # rt.observation hours, past the end of each hour
    raw_days_default: float  # raw archive days of a source without its own value
    raw_days_by_source: tuple[tuple[str, float], ...]
    history_days: int  # history service dates before today

    def raw_days(self, source: str) -> int:
        """Whole raw archive days kept of `source`."""

        return int(dict(self.raw_days_by_source).get(source, self.raw_days_default))

    @property
    def longest_raw_days(self) -> int:
        return int(max([self.raw_days_default, *(days for _, days in self.raw_days_by_source)]))


@dataclass(frozen=True, slots=True)
class Guard:
    """The free space `obehy jobs hourly` keeps on the data disk, and what it may delete for it;
    release loads leave the same room."""

    free_gb: int
    min_raw_days: int
    min_history_days: int


@dataclass(frozen=True, slots=True)
class PolicyValues:
    journey_namespaces: tuple[str, ...]  # a trip's journey key: the first of these it has
    vehicle_day_gap_s: int  # a vehicle's day ends at a gap this long (circulations)
    retention: Retention
    guard: Guard
    keyless_episode_gap_s: int  # the replay report's keyless episodes (`[keyless]`)


def read_policy(path: Path) -> PolicyValues:
    """The values read here of a policy file; nothing else in it is checked."""

    document = _document(path)
    where = f"{path}: "
    journey = _table(document, "journey", where)
    namespaces = journey.get("namespaces")
    if (
        not isinstance(namespaces, list)
        or not namespaces
        or any(not isinstance(n, str) or not n for n in cast(list[object], namespaces))
    ):
        raise ConfigError(f"{where}journey.namespaces must be a list of namespaces")
    time_table = _table(document, "time", where)
    retention = _table(document, "retention", where)
    raw_days = _table(retention, "raw_days", f"{where}retention.")
    guard = _table(document, "guard", where)
    keyless = _table(document, "keyless", where)
    return PolicyValues(
        journey_namespaces=tuple(cast(list[str], namespaces)),
        vehicle_day_gap_s=_integer(time_table, "vehicle_day_gap_s", f"{where}time."),
        retention=Retention(
            observation_hours=_integer(retention, "observation_hours", f"{where}retention."),
            raw_days_default=_number(raw_days, "default", f"{where}retention.raw_days."),
            raw_days_by_source=tuple(
                sorted(
                    (name, _number(raw_days, name, f"{where}retention.raw_days."))
                    for name in raw_days
                    if name != "default"
                )
            ),
            history_days=_integer(retention, "history_days", f"{where}retention."),
        ),
        guard=Guard(
            free_gb=_integer(guard, "free_gb", f"{where}guard."),
            min_raw_days=_integer(guard, "min_raw_days", f"{where}guard."),
            min_history_days=_integer(guard, "min_history_days", f"{where}guard."),
        ),
        keyless_episode_gap_s=_integer(keyless, "episode_gap_s", f"{where}keyless."),
    )


@cache
def policy_values(path: Path = POLICY) -> PolicyValues:
    """The shipped policy's values, read once."""

    return read_policy(path)


# --- Connector manifests -------------------------------------------------------------------------


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


def read_applies(table: Mapping[str, Any], where: str) -> Applies:
    """A lookup's `applies` table (absent: every trip, any day)."""

    if "applies" not in table:
        return Applies()
    applies = _table(table, "applies", f"{where}: ")
    keys = applies.get("keys", {})
    if not isinstance(keys, dict):
        raise ConfigError(f"{where}: applies.keys must be a table of patterns")
    patterns: list[tuple[str, str]] = []
    for namespace, pattern in sorted(cast(dict[str, object], keys).items()):
        if not isinstance(pattern, str):
            raise ConfigError(f"{where}: applies.keys.{namespace} must be a string")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ConfigError(f"{where}: applies.keys.{namespace}: {error}") from error
        patterns.append((namespace, pattern))
    days = applies.get("days_around")
    if days is not None and (not isinstance(days, int) or isinstance(days, bool) or days < 0):
        raise ConfigError(f"{where}: applies.days_around must be a whole number of days")
    return Applies(tuple(patterns), days)


@dataclass(frozen=True)
class Lookup:
    """A declared lazy detail (a composition): which trips it answers for. How it is asked is
    the worker's."""

    source: str
    name: str
    kind: str
    feeds: tuple[str, ...]
    applies: Applies = Applies()


@dataclass(frozen=True)
class Source:
    """How the public feed list names a connector: its manifest's `title` and `covers`, and the
    feeds any of its channels feeds live (in `FEED_NAMES` order)."""

    source: str
    title: str  # e.g. "DÚK vehicle positions (Ústecký kraj)"
    covers: str  # what it covers and contributes, one or two sentences
    live_feeds: tuple[str, ...]


def _manifest_files(path: Path) -> list[Path]:
    return sorted(path.glob("*.toml")) if path.is_dir() else [path]


def _tables(document: Mapping[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    value = document.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in cast(list[object], value)
    ):
        raise ConfigError(f"{where}: [[{key}]] must be an array of tables")
    return cast(list[dict[str, Any]], value)


def _live_feeds(channels: list[dict[str, Any]], where: str) -> tuple[str, ...]:
    live: set[str] = set()
    for number, channel in enumerate(channels, start=1):
        at = f"{where} channel {number}: "
        feeds = _names(channel, "feeds", at, FEED_NAMES)
        # No `live_feeds`: live on every feed it feeds.
        live.update(_names(channel, "live_feeds", at, feeds) if "live_feeds" in channel else feeds)
    return tuple(feed for feed in FEED_NAMES if feed in live)


def read_source(path: Path) -> tuple[Source, list[Lookup]]:
    """One manifest's description and declared lookups."""

    document = _document(path)
    where = str(path)
    source = _text(document, "source", f"{where}: ")
    if path.stem != source:
        raise ConfigError(f"{where}: source {source!r} must match the file name")
    described = Source(
        source=source,
        title=_text(document, "title", f"{where}: "),
        covers=_text(document, "covers", f"{where}: "),
        live_feeds=_live_feeds(_tables(document, "channel", where), where),
    )
    lookups = [
        Lookup(
            source=source,
            name=_text(table, "name", f"{where} lookup {number}: "),
            kind=_text(table, "kind", f"{where} lookup {number}: "),
            feeds=_names(table, "feeds", f"{where} lookup {number}: ", FEED_NAMES),
            applies=read_applies(table, f"{where} lookup {number}"),
        )
        for number, table in enumerate(_tables(document, "lookup", where), start=1)
    ]
    return described, lookups


def load_sources(path: Path = SOURCES) -> list[Source]:
    """Every connector manifest's description (by file name), or one manifest file's."""

    return [read_source(file)[0] for file in _manifest_files(path)]


def declared_lookups(path: Path = SOURCES) -> list[Lookup]:
    """Every `[[lookup]]` of the manifests (by file name), or of one manifest file."""

    return [lookup for file in _manifest_files(path) for lookup in read_source(file)[1]]
