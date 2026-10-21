"""The lookup broker (BASE_PLAN.md section 18.2): answers what the API was asked
(`rt.lookup_request`) through the lookup connectors.

The API records a request and NOTIFYs `obehy_lookup`; the broker wakes on it, or every
`SWEEP_S`, and for each pending request and each lookup of its kind covering the trip's feed:

1. reads what the worker knows of the trip (`sql/lookup_view.sql`) and asks the connector's
   `plan`: not available (another operator's train, no SR70), or an upstream request with a
   cache key;
2. reuses the answer of that key while it holds (`rt.detail_cache`: `ttl_s`, `not_found_ttl_s`,
   `retry_s` after a failure); otherwise fetches it, within the lookup's token bucket (a request
   without a token waits for a later pass) and daily budget (spent: failed);
3. archives the answer like a poll (`<source>/<lookup>` in the raw archive), logs it
   (`rt.lookup_fetch`, the recorded pairs), decodes it and writes the composition, merged with
   the journey's earlier answers (section 28).

A request is answered once every lookup has its outcome. Requests planned to one cache key share
one fetch, within a pass and across passes. Fetches run on the broker's thread, never the event
loop's: an upstream answer slow to come delays only other lookups.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast
from urllib.parse import quote

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.compositions import (
    JOURNEY_NAMESPACES,
    NotAvailable,
    Subject,
    TripView,
    Unit,
    UpstreamRequest,
    ViewCall,
)
from obehy.realtime.emit.compositions import write_composition
from obehy.realtime.manifest import Channel, Lookup, Rate
from obehy.realtime.sources import cd
from obehy.realtime.times import PRAGUE, Instant, instant, local_date

LOOKUP_CHANNEL = "obehy_lookup"
SWEEP_S = 5.0
BATCH = 50
SQL = Path(__file__).resolve().parent.parent / "sql"
# The keys lookups plan with.
TRIP_NAMESPACES = ("czptt:train_number", *JOURNEY_NAMESPACES)
LOCATION_NAMESPACES = ("sr70",)

Plan = Callable[[Subject, TripView], UpstreamRequest | NotAvailable]
Decode = Callable[[bytes, UpstreamRequest], tuple[Unit, ...]]
Fetcher = Callable[[Channel], Poll]
Clock = Callable[[], Instant]

CONNECTORS: dict[tuple[str, str], tuple[Plan, Decode]] = {
    (cd.SOURCE, cd.COMPOSITION): (cd.plan, cd.decode),
}


def _now() -> Instant:
    return instant(datetime.now(UTC))


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}", flush=True)


@dataclass
class Bucket:
    """A token bucket: `rate.per_minute` on average, at most `rate.burst` at once."""

    rate: Rate
    tokens: float = -1.0
    at: float = 0.0

    def take(self, now: float) -> bool:
        if self.tokens < 0:
            self.tokens, self.at = float(self.rate.burst), now
        self.tokens = min(
            float(self.rate.burst), self.tokens + (now - self.at) * self.rate.per_minute / 60
        )
        self.at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def fill(url: str, params: Sequence[tuple[str, str]]) -> str:
    for name, value in params:
        url = url.replace(f"{{{name}}}", quote(value, safe=""))
    return url


def _query(name: str) -> LiteralString:
    return cast(LiteralString, (SQL / f"{name}.sql").read_text(encoding="utf-8"))


def read_view(
    connection: psycopg.Connection[DictRow], trip_id: str, service_date: date, now: Instant
) -> TripView | None:
    row = connection.execute(
        _query("lookup_view"),
        {
            "trip_id": trip_id,
            "service_date": service_date,
            "trip_namespaces": list(TRIP_NAMESPACES),
            "location_namespaces": list(LOCATION_NAMESPACES),
        },
    ).fetchone()
    if row is None:
        return None
    calls = tuple(
        ViewCall(
            ordinal=c["ordinal"],
            location_id=c["location_id"],
            visit_n=c["visit_n"],
            passenger=c["passenger"],
            left=c["left"],
            keys=cast(dict[str, str], c["keys"]),
        )
        for c in cast(list[dict[str, Any]], row["calls"])
    )
    return TripView(
        row["feed"], trip_id, service_date, now, cast(dict[str, str], row["keys"]), calls
    )


@dataclass
class LookupBroker:
    """One pass at a time over the pending requests (`serve`); its own connection."""

    connection: psycopg.Connection[DictRow]
    lookups: Sequence[Lookup]
    fetcher: Fetcher
    archive: ArchiveWriter
    clock: Clock = _now
    monotonic: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = _log
    buckets: dict[str, Bucket] = field(default_factory=dict[str, Bucket])
    spent: dict[str, tuple[date, int]] = field(default_factory=dict[str, tuple[date, int]])

    def __post_init__(self) -> None:
        self.lookups = [lookup for lookup in self.lookups if self._known(lookup)]
        for lookup in self.lookups:
            self.buckets[lookup.qualified] = Bucket(lookup.rate)

    def _known(self, lookup: Lookup) -> bool:
        if (lookup.source, lookup.name) in CONNECTORS:
            return True
        self.log(f"{lookup.qualified}: no connector; not asked")
        return False

    def serve(self) -> int:
        """Answer what can be answered now; returns the requests answered."""

        pending = self.connection.execute(
            "SELECT kind, feed, trip_id, service_date, from_location, outcomes"
            " FROM rt.lookup_request WHERE pending ORDER BY requested_at LIMIT %s",
            (BATCH,),
        ).fetchall()
        answered = 0
        views: dict[tuple[str, date], TripView | None] = {}
        for row in pending:
            now = self.clock()
            covering = [
                lookup
                for lookup in self.lookups
                if lookup.kind == row["kind"] and row["feed"] in lookup.feeds
            ]
            subject = Subject(
                row["feed"], row["trip_id"], row["service_date"], row["from_location"] or None
            )
            place = (subject.trip_id, subject.service_date)
            if place not in views:
                views[place] = read_view(self.connection, *place, now)
            view = views[place]
            outcomes = cast(dict[str, Any], dict(row["outcomes"]))
            for lookup in covering:
                if lookup.qualified in outcomes:
                    continue
                if view is None:
                    outcomes[lookup.qualified] = {
                        "outcome": "not_available",
                        "reason": "the trip is not in the active release",
                    }
                    continue
                outcome = self._answer(lookup, subject, view, now)
                if outcome is not None:
                    outcomes[lookup.qualified] = outcome
            done = all(lookup.qualified in outcomes for lookup in covering)
            self.connection.execute(
                "UPDATE rt.lookup_request SET outcomes = %s, pending = %s,"
                " answered_at = CASE WHEN %s THEN statement_timestamp() ELSE answered_at END"
                " WHERE (kind, feed, trip_id, service_date, from_location) = (%s, %s, %s, %s, %s)",
                (
                    Jsonb(outcomes),
                    not done,
                    done,
                    row["kind"],
                    row["feed"],
                    row["trip_id"],
                    row["service_date"],
                    row["from_location"],
                ),
            )
            answered += done
        return answered

    def _answer(
        self, lookup: Lookup, subject: Subject, view: TripView, now: Instant
    ) -> dict[str, Any] | None:
        """The lookup's outcome for the subject; None to wait for a token."""

        plan, decode = CONNECTORS[(lookup.source, lookup.name)]
        planned = plan(subject, view)
        if isinstance(planned, NotAvailable):
            return {"outcome": "not_available", "reason": planned.reason}
        cached = self.connection.execute(
            "SELECT outcome, fetch_id FROM rt.detail_cache"
            " WHERE source = %s AND lookup = %s AND cache_key = %s AND expires_at > %s",
            (lookup.source, lookup.name, planned.cache_key, now),
        ).fetchone()
        if cached is not None:
            return {"outcome": cached["outcome"], "cache_key": planned.cache_key}
        if not self.buckets[lookup.qualified].take(self.monotonic()):
            return None
        if not self._spend(lookup, now):
            return {
                "outcome": "failed",
                "reason": "the daily budget is spent",
                "cache_key": planned.cache_key,
            }
        return self._fetch(lookup, view, planned, decode)

    def _spend(self, lookup: Lookup, now: Instant) -> bool:
        today = local_date(now)
        day, spent = self.spent.get(lookup.qualified, (None, 0))
        if day != today:
            midnight = datetime.combine(today, datetime.min.time(), PRAGUE)
            row = self.connection.execute(
                "SELECT count(*) AS n FROM rt.lookup_fetch"
                " WHERE source = %s AND lookup = %s AND requested_at >= %s",
                (lookup.source, lookup.name, midnight),
            ).fetchone()
            spent = 0 if row is None else int(row["n"])
        if spent >= lookup.daily_budget:
            self.spent[lookup.qualified] = (today, spent)
            return False
        self.spent[lookup.qualified] = (today, spent + 1)
        return True

    def _fetch(
        self, lookup: Lookup, view: TripView, planned: UpstreamRequest, decode: Decode
    ) -> dict[str, Any]:
        request = replace(lookup.request, url=fill(lookup.request.url, planned.params))
        poll = self.fetcher(request)
        extra = {"request": {"cache_key": planned.cache_key, **dict(planned.params)}}
        stored = self.archive.append(lookup.source, lookup.name, poll, poll.body, extra)
        units: tuple[Unit, ...] = ()
        error = poll.error if not poll.ok else None
        if error is None and poll.body is not None:
            try:
                units = decode(poll.body, planned)
            except ValueError as decode_error:
                error = f"undecodable answer: {decode_error}"
        outcome = "failed" if error is not None else "answered" if units else "not_found"
        if error is not None:
            self.log(f"{lookup.qualified} {planned.cache_key}: {error}")
        received = instant(poll.received_at)
        logged = self.connection.execute(
            "INSERT INTO rt.lookup_fetch (source, lookup, cache_key, requested_at, received_at,"
            " status, error, raw_sha256, outcome) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING fetch_id",
            (
                lookup.source,
                lookup.name,
                planned.cache_key,
                poll.requested_at,
                poll.received_at,
                poll.status,
                error,
                stored.sha256,
                outcome,
            ),
        ).fetchone()
        assert logged is not None
        fetch_id = int(logged["fetch_id"])
        composition_id = None
        if outcome == "answered":
            composition_id = write_composition(
                self.connection, view, lookup.source, planned.start, units, received, fetch_id
            )
        hold = {
            "answered": lookup.ttl_s,
            "not_found": lookup.not_found_ttl_s,
            "failed": lookup.retry_s,
        }[outcome]
        self.connection.execute(
            "INSERT INTO rt.detail_cache (source, lookup, cache_key, fetch_id, outcome,"
            " fetched_at, expires_at, composition_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (source, lookup, cache_key) DO UPDATE SET fetch_id = excluded.fetch_id,"
            " outcome = excluded.outcome, fetched_at = excluded.fetched_at,"
            " expires_at = excluded.expires_at, composition_id = excluded.composition_id",
            (
                lookup.source,
                lookup.name,
                planned.cache_key,
                fetch_id,
                outcome,
                received,
                received + timedelta(seconds=hold),
                composition_id,
            ),
        )
        answer: dict[str, Any] = {"outcome": outcome, "cache_key": planned.cache_key}
        if error is not None:
            answer["reason"] = error
        return answer


async def run_broker(
    broker: LookupBroker, database_url: str, stop: asyncio.Event, sweep_s: float = SWEEP_S
) -> None:
    """Serve on every `NOTIFY obehy_lookup`, and every `sweep_s` (tokens refill, requests
    waiting for one)."""

    listener = await asyncio.to_thread(psycopg.connect, database_url, autocommit=True)
    listener.execute(f"LISTEN {LOOKUP_CHANNEL}")
    try:
        while not stop.is_set():
            await asyncio.to_thread(broker.serve)
            await asyncio.to_thread(lambda: list(listener.notifies(timeout=sweep_s, stop_after=1)))
    finally:
        listener.close()


def open_broker(
    database_url: str, lookups: Sequence[Lookup], fetcher: Fetcher, archive: Path
) -> LookupBroker:
    connection = psycopg.Connection[DictRow].connect(
        database_url, autocommit=True, row_factory=dict_row
    )
    connection.execute("SET TIME ZONE 'UTC'")
    return LookupBroker(connection, lookups, fetcher, ArchiveWriter(archive))


def prune_lookups(connection: psycopg.Connection[Any], before: datetime) -> int:
    """Delete requests, fetch records and cached answers older than `before`; compositions are
    kept. Returns the requests deleted."""

    deleted = connection.execute(
        "DELETE FROM rt.lookup_request WHERE requested_at < %s", (before,)
    ).rowcount
    connection.execute("DELETE FROM rt.lookup_fetch WHERE requested_at < %s", (before,))
    connection.execute("DELETE FROM rt.detail_cache WHERE expires_at < %s", (before,))
    return deleted
