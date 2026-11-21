"""Set-wise jobs over stored realtime data (`obehy jobs …`): retention and the disk guard.

They read what the worker leaves, by its names: the raw archive's day directories
(`<root>/<source>/<channel>/<YYYY-MM-DD>/`), the partitions of `rt.observation` (per UTC hour,
`observation_YYYYMMDDHH`; older servers' daily `observation_YYYYMMDD` until dropped) and of every
history table (per service date, `<table>_YYYYMMDD`), withdrawn alerts and old lookup requests.
Nothing here imports the worker: these jobs stay Python after the switch to the Rust worker
(docs/RUST_PORT.md).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import sql

from obehy.realtime.disk_guard import GB, next_step
from obehy.realtime_config import PolicyValues, instant, local_date

HISTORY_TABLES = (
    "journey",
    "journey_schedule",
    "journey_call",
    "actual_stop_event",
    "vehicle_assignment",
    "journey_link",
    "platform_evidence",
    "journey_cancellation",
)

_OBSERVATION = re.compile(r"^observation_(\d{8})(\d{2})?$")
_HISTORY_DAY = re.compile(r"^(?P<table>[a-z_]+)_(?P<day>\d{8})$")


def observation_end(name: str) -> datetime | None:
    """When an observation partition's range ends (hourly or the older daily), if it is one."""

    match = _OBSERVATION.match(name)
    if match is None:
        return None
    day = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=UTC)
    if match.group(2) is None:
        return day + timedelta(days=1)
    return day + timedelta(hours=int(match.group(2)) + 1)


def history_day(name: str) -> date | None:
    """The service date of a daily history partition, if `name` is one."""

    match = _HISTORY_DAY.match(name)
    if match is None or match.group("table") not in HISTORY_TABLES:
        return None
    day = match.group("day")
    return date(int(day[:4]), int(day[4:6]), int(day[6:]))


def archive_days(root: Path) -> list[date]:
    """The UTC days any source or channel of the raw archive has a directory of, oldest first."""

    found: set[date] = set()
    for directory in root.glob("*/*/*"):
        try:
            day = date.fromisoformat(directory.name)
        except ValueError:
            continue
        if directory.is_dir():
            found.add(day)
    return sorted(found)


def archive_sources(root: Path) -> list[str]:
    """The sources with a directory in the raw archive."""

    return sorted(directory.name for directory in root.glob("*") if directory.is_dir())


def prune_archive(root: Path, before: date, source: str = "*") -> list[Path]:
    """Delete the raw archive's day directories of every channel of `source` (every source by
    default) older than `before`; returns them.

    Only `<root>/<source>/<channel>/<YYYY-MM-DD>` directories are touched.
    """

    pruned: list[Path] = []
    for directory in sorted(root.glob(f"{source}/*/*")):
        try:
            day = date.fromisoformat(directory.name)
        except ValueError:
            continue
        if directory.is_dir() and day < before:
            shutil.rmtree(directory)
            pruned.append(directory)
    return pruned


def _partitions(connection: psycopg.Connection[Any], parent: str) -> list[str]:
    rows = connection.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = %s::regclass ORDER BY 1",
        (parent,),
    ).fetchall()
    return [cast(str, name) for (name,) in rows]


def drop_observations(connection: psycopg.Connection[Any], before: datetime) -> list[str]:
    """Drop the `rt.observation` partitions (hourly, or daily from older servers) that end at
    or before `before`."""

    dropped: list[str] = []
    for name in _partitions(connection, "rt.observation"):
        end = observation_end(name)
        if end is not None and end <= before:
            connection.execute(sql.SQL("DROP TABLE rt.{}").format(sql.Identifier(name)))
            dropped.append(name)
    return dropped


def history_days(connection: psycopg.Connection[Any]) -> list[date]:
    """The service dates history has partitions of, oldest first."""

    found = {
        day
        for table in HISTORY_TABLES
        for name in _partitions(connection, f"history.{table}")
        if (day := history_day(name)) is not None
    }
    return sorted(found)


def drop_history(connection: psycopg.Connection[Any], before: date) -> list[date]:
    """Drop every history table's partitions of service dates before `before`, and the
    compositions of those dates (a record like history, kept as long); returns the history
    dates dropped."""

    dropped: set[date] = set()
    with connection.transaction():
        # Units go with their composition (ON DELETE CASCADE).
        connection.execute("DELETE FROM rt.composition WHERE service_date < %s", (before,))
        for table in HISTORY_TABLES:
            for name in _partitions(connection, f"history.{table}"):
                day = history_day(name)
                if day is not None and day < before:
                    connection.execute(
                        sql.SQL("DROP TABLE history.{}").format(sql.Identifier(name))
                    )
                    dropped.add(day)
    return sorted(dropped)


def prune_alerts(connection: psycopg.Connection[Any], before: datetime) -> int:
    """Delete alerts withdrawn before `before`; how many."""

    deleted = connection.execute("DELETE FROM rt.alert WHERE withdrawn_at < %s", (before,))
    return deleted.rowcount


def prune_lookups(connection: psycopg.Connection[Any], before: datetime) -> int:
    """Delete requests, fetch records and cached answers older than `before`; compositions are
    kept. Returns the requests deleted."""

    deleted = connection.execute(
        "DELETE FROM rt.lookup_request WHERE requested_at < %s", (before,)
    ).rowcount
    connection.execute("DELETE FROM rt.lookup_fetch WHERE requested_at < %s", (before,))
    connection.execute("DELETE FROM rt.detail_cache WHERE expires_at < %s", (before,))
    return deleted


def hourly(
    connection: psycopg.Connection[Any],
    archive: Path,
    policy: PolicyValues,
    now: datetime,
    free: Callable[[], int],
) -> Iterator[str]:
    """Drop observation hours past the retention, then keep the data disk's free space above
    the guard; yields what it did. `free` measures the free bytes now."""

    cutoff = now - timedelta(hours=policy.retention.observation_hours)
    dropped = drop_observations(connection, cutoff)
    yield f"dropped {len(dropped)} observation partitions ending by {cutoff:%Y-%m-%d %H:%M} UTC"
    while True:
        available = free()
        step = next_step(available, policy.guard, archive_days(archive), history_days(connection))
        if step is None:
            if available < policy.guard.free_gb * GB:
                yield (
                    f"disk guard: {available / GB:.1f} GB free, below {policy.guard.free_gb} GB,"
                    " and nothing more may be deleted"
                )
            return
        if step.kind == "raw":
            prune_archive(archive, step.day + timedelta(days=1))
        else:
            drop_history(connection, step.day + timedelta(days=1))
        yield f"disk guard: {available / GB:.1f} GB free, deleted the {step.kind} day {step.day}"


def nightly(
    connection: psycopg.Connection[Any], archive: Path, policy: PolicyValues, now: datetime
) -> Iterator[str]:
    """Drop the history days past the retention (with their compositions), the alerts and
    lookup requests past the longest raw archive window, and each source's raw archive days
    past its own; yields what it did."""

    kept = local_date(instant(now)) - timedelta(days=policy.retention.history_days)
    dropped = drop_history(connection, kept)
    yield f"dropped {len(dropped)} history days before {kept}"
    # Alerts and lookup requests outlive the raw archive's shorter windows.
    withdrawn = now - timedelta(days=policy.retention.longest_raw_days)
    yield f"deleted {prune_alerts(connection, withdrawn)} alerts withdrawn before {withdrawn}"
    yield f"deleted {prune_lookups(connection, withdrawn)} lookup requests before {withdrawn}"
    for source in archive_sources(archive):
        cutoff = now.date() - timedelta(days=policy.retention.raw_days(source))
        pruned = prune_archive(archive, cutoff, source)
        yield f"deleted {len(pruned)} raw archive days of {source} before {cutoff}"
