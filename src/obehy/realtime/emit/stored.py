"""Reading stored observations back for warm replay (docs/R1_SLICE.md section 5).

The window is read in `received_at` order, which the `observation_time` index streams without a
sort; a chunk never splits observations received at the same instant, so sorting each chunk by
the full processing key (`Runner.process`) gives the same order as one sort over the window.
`stream_observations` reads and decodes on a thread with its own connection, so waiting on the
database and decoding the facts overlap with the core.

With `alive` (policy `warm_replay.mode = "vehicles"`) only what the state of the vehicles still
live depends on is read: a vehicle is live when its last observation was received at or after
`alive`; its observations are read from its `state_since` (its binding's or keyless episode's
start), and every observation of the journeys live vehicles or recent station boards are on, so
each journey is rebuilt from its first observation, whichever vehicle or source made it. Rows
stored before `state_since` existed are always read.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

import psycopg

from obehy.realtime.model import Feed, Observation, RawRef
from obehy.realtime.model_json import facts_from_json
from obehy.realtime.times import instant

CHUNK = 20_000
DEPTH = 4  # decoded chunks the reader may run ahead of the core


_COLUMNS = (
    "o.source, o.channel, o.feed, o.received_at, o.observed_at, o.raw_sha256, o.raw_item,"
    " o.decoder_version, o.facts"
)

_WINDOW = f"""
SELECT {_COLUMNS} FROM rt.observation o
WHERE o.received_at >= %(since)s AND o.received_at < %(until)s
ORDER BY o.received_at
"""

_BY_VEHICLE = f"""
WITH latest AS (
    SELECT DISTINCT ON (source, vehicle_id) source, vehicle_id, received_at, state_since,
        feed, journey_namespace, journey_key, service_date
    FROM rt.observation
    WHERE received_at >= %(since)s AND received_at < %(until)s AND vehicle_id IS NOT NULL
    ORDER BY source, vehicle_id, received_at DESC
), live AS (
    SELECT * FROM latest WHERE received_at >= %(alive)s
), journeys AS (
    SELECT feed, journey_namespace, journey_key, service_date
    FROM live WHERE journey_key IS NOT NULL
    UNION
    SELECT feed, journey_namespace, journey_key, service_date
    FROM rt.observation
    WHERE received_at >= %(alive)s AND received_at < %(until)s AND vehicle_id IS NULL
        AND state_since IS NOT NULL AND journey_key IS NOT NULL
)
SELECT {_COLUMNS} FROM rt.observation o
WHERE o.received_at >= %(since)s AND o.received_at < %(until)s AND (
    o.state_since IS NULL
    OR EXISTS (
        SELECT 1 FROM live l
        WHERE l.source = o.source AND l.vehicle_id = o.vehicle_id
            AND o.received_at >= l.state_since
    )
    OR EXISTS (
        SELECT 1 FROM journeys j
        WHERE j.feed = o.feed AND j.journey_namespace = o.journey_namespace
            AND j.journey_key = o.journey_key AND j.service_date = o.service_date
    )
)
ORDER BY o.received_at
"""


def stored_observations(
    connection: psycopg.Connection,
    since: datetime,
    until: datetime,
    alive: datetime | None = None,
    chunk: int = CHUNK,
) -> Iterator[list[Observation]]:
    """Stored observations received in [since, until), in `received_at` order, about `chunk` at
    a time; read through a server-side cursor, so hours of them are never in memory at once.
    With `alive`, only those the vehicles still live depend on (see the module)."""

    query = _WINDOW if alive is None else _BY_VEHICLE
    with (
        connection.transaction(),
        connection.cursor(name="warm_replay") as cursor,
    ):
        cursor.execute(query, {"since": since, "until": until, "alive": alive})
        batches = iter(lambda: [_stored(*row) for row in cursor.fetchmany(chunk)], [])
        yield from whole_instants(batches)


def whole_instants(batches: Iterable[list[Observation]]) -> Iterator[list[Observation]]:
    """`received_at`-ordered batches regrouped so no instant spans two of them: the
    observations of a batch's last instant wait for the next batch, where more may follow."""

    pending: list[Observation] = []
    for batch in batches:
        pending.extend(batch)
        if not pending:
            continue
        last = pending[-1].received_at
        cut = len(pending)
        while cut and pending[cut - 1].received_at == last:
            cut -= 1
        if cut:
            yield pending[:cut]
            pending = pending[cut:]
    if pending:
        yield pending


def stream_observations(
    database_url: str,
    since: datetime,
    until: datetime,
    alive: datetime | None = None,
    chunk: int = CHUNK,
) -> Iterator[list[Observation]]:
    """`stored_observations` read ahead on a thread with its own connection. Closing the
    iterator early stops the reader; a reader error is raised here."""

    chunks: queue.Queue[list[Observation] | BaseException | None] = queue.Queue(DEPTH)
    cancelled = threading.Event()

    def put(item: list[Observation] | BaseException | None) -> bool:
        while not cancelled.is_set():
            try:
                chunks.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def read() -> None:
        try:
            with psycopg.connect(database_url, autocommit=True) as connection:
                for part in stored_observations(connection, since, until, alive, chunk):
                    if not put(part):
                        return
        except BaseException as error:  # handed to the consumer, raised there
            put(error)
            return
        put(None)

    reader = threading.Thread(target=read, name="warm-replay-read", daemon=True)
    reader.start()
    try:
        while (item := chunks.get()) is not None:
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        cancelled.set()
        reader.join()


def _stored(
    source: str,
    channel: str,
    feed: Feed,
    received_at: datetime,
    observed_at: datetime | None,
    sha256: str,
    item: int,
    version: int,
    facts: Any,
) -> Observation:
    return Observation(
        source=source,
        channel=channel,
        feed=feed,
        received_at=instant(received_at),
        observed_at=None if observed_at is None else instant(observed_at),
        raw=RawRef(sha256, item),
        decoder_version=version,
        facts=facts_from_json(facts),
    )
