"""Demand polling: request what is needed now, within a budget (docs/R2_SLICE.md section 7).

The first demand channel is SŽ's station boards (`station_boards`): one request per station,
covering about an hour of its trains. Due passenger calls at stations with at least
`boards.min_boarding_points` boarding points come from the active release in SQL (refreshed
every `due_refresh_s`); each minute the planner picks the stations to read now:

- once when a call comes within `early_min`;
- again within `near_min` when the last read is older than `near_refresh_s`;
- again within `imminent_min` when it is older than `imminent_refresh_s`.

Reads go one at a time, spread over the minute, the soonest due call first (never-read stations
before refreshes), at most `ceiling_per_min`; the rest of the minute's demand is dropped and
counted. A refusal (403/429), a server error or an answer slower than `slow_latency_factor`
times the median halves the ceiling for `slowdown_s`. A 403 also opens the channel's circuit
(the scheduler's rule). The request's body has `{sr70}` replaced by the station's 6-digit code,
kept with the poll for the decoder.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import statistics
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel
from obehy.realtime.policy import BoardPolicy
from obehy.realtime.runtime.scheduler import BLOCKED, BLOCKED_STEPS
from obehy.realtime.times import Instant, instant, local_date

CATALOGUE = Path(__file__).resolve().parents[2] / "data" / "realtime" / "sr70-name20.csv"

DueCalls = Mapping[str, Sequence[Instant]]  # 6-digit SR70 -> due times, ascending
FetchBody = Callable[[Channel, bytes], Poll]
OnDemandPoll = Callable[[Channel, Poll], Awaitable[None]]


def full_codes() -> dict[str, str]:
    """5-digit SR70 → the 6-digit code with check digit, from the catalogue (never computed:
    the check digit is not always Luhn). Codes the catalogue does not make unique are left out."""

    found: dict[str, set[str]] = {}
    with CATALOGUE.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            if row and len(row[0]) == 6 and row[0].isdigit():
                found.setdefault(row[0][:5], set()).add(row[0])
    return {short: next(iter(full)) for short, full in found.items() if len(full) == 1}


# --- planning (pure) ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReadLog:
    """When each station was last read; demand dropped over the ceiling."""

    last: dict[str, Instant] = field(default_factory=dict[str, Instant])
    dropped: int = 0


def plan_reads(
    now: Instant, due: DueCalls, log: ReadLog, policy: BoardPolicy, budget: int
) -> list[str]:
    """The stations to read in the coming minute, most urgent first, at most `budget`."""

    early = timedelta(minutes=policy.early_min)
    near = timedelta(minutes=policy.near_min)
    imminent = timedelta(minutes=policy.imminent_min)
    wanted: list[tuple[bool, Instant, str]] = []
    for station, times in due.items():
        upcoming = [t for t in times if t >= now]
        if not upcoming:
            continue
        soonest = upcoming[0]
        if soonest - now > early:
            continue
        last = log.last.get(station)
        unread = last is None or last < soonest - early
        stale = last is not None and (
            (
                soonest - now <= imminent
                and now - last > timedelta(seconds=policy.imminent_refresh_s)
            )
            or (soonest - now <= near and now - last > timedelta(seconds=policy.near_refresh_s))
        )
        if unread or stale:
            wanted.append((not unread, soonest, station))
    wanted.sort()
    chosen = [station for _, _, station in wanted[:budget]]
    log.dropped += max(0, len(wanted) - budget)
    return chosen


@dataclass(slots=True)
class Pace:
    """The budget: halved for `slowdown_s` after a refusal, a server error or a slow answer."""

    policy: BoardPolicy
    latencies: list[float] = field(default_factory=list[float])
    slow_until: Instant | None = None

    def budget(self, now: Instant) -> int:
        ceiling = self.policy.ceiling_per_min
        if self.slow_until is not None and now < self.slow_until:
            return max(1, ceiling // 2)
        return ceiling

    def note(self, poll: Poll, now: Instant) -> None:
        latency = (poll.received_at - poll.requested_at).total_seconds()
        slow = len(self.latencies) >= 10 and latency > self.policy.slow_latency_factor * (
            statistics.median(self.latencies)
        )
        self.latencies = [*self.latencies[-99:], latency]
        refused = poll.status in (429, BLOCKED) or (poll.status or 0) >= 500
        if refused or slow:
            self.slow_until = Instant(now + timedelta(seconds=self.policy.slowdown_s))


# --- due calls (SQL) ---------------------------------------------------------------------------


class StationDemand:
    """Due passenger calls per station of the active rail release, refreshed periodically."""

    def __init__(self, connection: psycopg.Connection, load_id: int, policy: BoardPolicy) -> None:
        self.connection = connection
        self.load_id = load_id
        self.policy = policy
        self.codes = full_codes()
        self.stations: dict[str, str] | None = None  # location -> 5-digit SR70
        self.cached: dict[str, list[Instant]] = {}
        self.until: Instant | None = None

    def switch(self, load_id: int) -> None:
        """A new release was activated: query its calls from now on."""

        self.load_id = load_id
        self.stations = None
        self.until = None

    def _stations(self) -> dict[str, str]:
        if self.stations is None:
            rows = self.connection.execute(
                "SELECT k.public_id, k.identifier FROM static.source_key k"
                " WHERE k.load_id = %s AND k.namespace = 'sr70' AND k.public_id IN ("
                "  SELECT location_id FROM static.trip_call WHERE load_id = %s"
                "  AND passenger_service AND boarding_point_id IS NOT NULL"
                "  AND boarding_point_id NOT LIKE %s"
                "  GROUP BY location_id HAVING count(DISTINCT boarding_point_id) >= %s)",
                (self.load_id, self.load_id, "%:BUS", self.policy.min_boarding_points),
            ).fetchall()
            self.stations = {str(location): str(code) for location, code in rows}
        return self.stations

    def due(self, now: Instant) -> DueCalls:
        if self.until is not None and now < self.until:
            return self.cached
        horizon = timedelta(minutes=self.policy.window_min) + timedelta(
            seconds=self.policy.due_refresh_s
        )
        stations = self._stations()
        today = local_date(now)
        rows = self.connection.execute(
            "SELECT tc.location_id, control.obehy_instant(sd.service_date,"
            "   coalesce(tc.scheduled_departure, tc.scheduled_arrival)) AS due"
            " FROM static.trip_call tc"
            " JOIN static.trip t ON t.load_id = tc.load_id AND t.trip_id = tc.trip_id"
            " JOIN static.service_date sd ON sd.load_id = t.load_id"
            "  AND sd.service_id = t.service_id AND sd.service_date = ANY(%s)"
            " WHERE tc.load_id = %s AND tc.passenger_service AND tc.location_id = ANY(%s)"
            "  AND control.obehy_instant(sd.service_date,"
            "   coalesce(tc.scheduled_departure, tc.scheduled_arrival)) BETWEEN %s AND %s"
            " ORDER BY 1, 2",
            (
                [today - timedelta(days=1), today],
                self.load_id,
                sorted(stations),
                now,
                now + horizon,
            ),
        ).fetchall()
        cached: dict[str, list[Instant]] = {}
        for location, at in rows:
            full = self.codes.get(stations[str(location)])
            if full is not None:
                cached.setdefault(full, []).append(instant(at))
        self.cached = cached
        self.until = Instant(now + timedelta(seconds=self.policy.due_refresh_s))
        return cached


# --- the loop ----------------------------------------------------------------------------------


def _now() -> Instant:
    return instant(datetime.now(UTC))


async def run_demand(
    channel: Channel,
    stop: asyncio.Event,
    demand: Callable[[Instant], DueCalls],
    fetch: FetchBody,
    on_poll: OnDemandPoll,
    policy: BoardPolicy,
    *,
    clock: Callable[[], Instant] = _now,
    log: ReadLog | None = None,
    once: bool = False,
) -> ReadLog:
    """Read the planned stations minute by minute until `stop`; one pass with `once`."""

    reads = log or ReadLog()
    pace = Pace(policy)
    blocked_until: Instant | None = None
    while not stop.is_set():
        started = clock()
        chosen: list[str] = []
        if blocked_until is None or started >= blocked_until:
            due = await asyncio.to_thread(demand, started)
            chosen = plan_reads(started, due, reads, policy, pace.budget(started))
        spacing = 60.0 / max(1, len(chosen))
        for station in chosen:
            if stop.is_set():
                break
            body = (channel.body or b"").replace(b"{sr70}", station.encode())
            poll = await asyncio.to_thread(fetch, channel, body)
            poll = replace(poll, request={"sr70": station})
            now = clock()
            reads.last[station] = now
            pace.note(poll, now)
            await on_poll(channel, poll)
            if poll.status == BLOCKED:  # SŽ refuses us: stop for the full backoff (circuit)
                blocked_until = Instant(now + timedelta(seconds=channel.max_backoff_s))
                break
            if not once:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=spacing)
        if once:
            return reads
        rest = 60.0 - (clock() - started).total_seconds()
        if rest > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=rest)
    return reads


__all__ = ["BLOCKED_STEPS", "Pace", "ReadLog", "StationDemand", "plan_reads", "run_demand"]
