"""Reading stored observations back for warm replay (docs/R1_SLICE.md section 5).

The window is read in `received_at` order, which the `observation_time` index streams without a
sort; a chunk never splits observations received at the same instant, so sorting each chunk by
the full processing key (`Runner.process`) gives the same order as one sort over the window.
`stream_observations` reads and decodes on a thread with its own connection, so waiting on the
database and decoding the facts overlap with the core.
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


def stored_observations(
    connection: psycopg.Connection, since: datetime, until: datetime, chunk: int = CHUNK
) -> Iterator[list[Observation]]:
    """Stored observations received in [since, until), in `received_at` order, about `chunk` at
    a time; read through a server-side cursor, so hours of them are never in memory at once."""

    with (
        connection.transaction(),
        connection.cursor(name="warm_replay") as cursor,
    ):
        cursor.execute(
            "SELECT source, channel, feed, received_at, observed_at, raw_sha256, raw_item,"
            " decoder_version, facts FROM rt.observation"
            " WHERE received_at >= %s AND received_at < %s ORDER BY received_at",
            (since, until),
        )
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
    database_url: str, since: datetime, until: datetime, chunk: int = CHUNK
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
                for part in stored_observations(connection, since, until, chunk):
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
