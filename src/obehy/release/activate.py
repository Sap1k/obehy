"""``obehy release activate``: switch the active release, roll back, and prune old loads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from obehy.release.contract import Contract
from obehy.release.ddl import active_view, static_tables
from obehy.release.load import partition_name
from obehy.release.pairing import pair, summary

NOTIFY_CHANNEL = "obehy_publication"
# Predecessors whose data stays loaded for `--rollback`; each release is about 7 GB.
DEFAULT_KEEP = 0


class ActivationError(RuntimeError):
    """A release cannot be activated or rolled back."""


@dataclass(frozen=True)
class Publication:
    seq: int
    run_id: str
    jdf_load_id: int
    czptt_load_id: int

    @property
    def load_ids(self) -> tuple[int, int]:
        return (self.jdf_load_id, self.czptt_load_id)


def _current(connection: psycopg.Connection, *, lock: bool = False) -> Publication | None:
    row = connection.execute(
        "SELECT history_seq, run_id, jdf_load_id, czptt_load_id FROM control.publication"
        + (" FOR UPDATE" if lock else "")
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return Publication(*cast(tuple[int, str, int, int], row))


def _history(connection: psycopg.Connection, seq: int) -> dict[str, Any]:
    with connection.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            "SELECT seq, run_id, jdf_load_id, czptt_load_id, previous_seq"
            " FROM control.publication_history WHERE seq = %s",
            (seq,),
        ).fetchone()
    if row is None:
        raise ActivationError(f"Publication history entry {seq} does not exist")
    return row


def _publish(
    connection: psycopg.Connection,
    contract: Contract,
    run_id: str,
    load_ids: tuple[int, int],
    *,
    action: str,
    previous_seq: int | None,
) -> Publication:
    """Record the publication and point every active view at its loads. Caller holds the lock."""

    statuses = dict(
        connection.execute(
            "SELECT load_id, status FROM control.load WHERE load_id = ANY(%s)", (list(load_ids),)
        ).fetchall()
    )
    unusable = [load_id for load_id in load_ids if statuses.get(load_id) != "loaded"]
    if unusable:
        raise ActivationError(f"Loads {unusable} of {run_id} are not loaded (dropped or failed)")
    row = connection.execute(
        "INSERT INTO control.publication_history"
        " (run_id, jdf_load_id, czptt_load_id, action, previous_seq)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING seq",
        (run_id, *load_ids, action, previous_seq),
    ).fetchone()
    seq = cast(int, row[0] if row else None)
    connection.execute(
        "UPDATE control.publication SET history_seq = %s, run_id = %s, jdf_load_id = %s,"
        " czptt_load_id = %s, activated_at = now()",
        (seq, run_id, *load_ids),
    )
    for table in static_tables(contract):
        connection.execute(active_view(table, load_ids).encode("utf-8"))
    # Which stop each station shares its board with (kept for a rollback to the same loads).
    pair(connection, load_ids)
    connection.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, run_id))
    return Publication(seq, run_id, *load_ids)


def activate(connection: psycopg.Connection, contract: Contract, run_id: str) -> Publication:
    """Activate the newest loaded jdf and czptt loads of a release in one transaction."""

    with connection.transaction():
        current = _current(connection, lock=True)
        rows = dict(
            connection.execute(
                "SELECT package, max(load_id) FROM control.load"
                " WHERE run_id = %s AND status = 'loaded' GROUP BY package",
                (run_id,),
            ).fetchall()
        )
        missing = [package for package in ("jdf", "czptt") if package not in rows]
        if missing:
            raise ActivationError(f"Release {run_id} has no loaded {' or '.join(missing)} package")
        load_ids = (cast(int, rows["jdf"]), cast(int, rows["czptt"]))
        if current is not None and current.load_ids == load_ids:
            raise ActivationError(f"Release {run_id} (loads {load_ids}) is already active")
        return _publish(
            connection,
            contract,
            run_id,
            load_ids,
            action="activate",
            previous_seq=current.seq if current else None,
        )


def activate_if_newer(
    connection: psycopg.Connection, contract: Contract, run_id: str
) -> Publication | None:
    """Activate a release only if it is newer than every release published before.

    Run ids start with their UTC build time, so they order by age. Comparing with the whole
    history, not the active release, keeps a scheduled update from undoing a rollback."""

    with connection.transaction():
        _current(connection, lock=True)
        row = connection.execute("SELECT max(run_id) FROM control.publication_history").fetchone()
        newest = cast(str | None, row[0] if row else None)
        if newest is not None and run_id <= newest:
            return None
        return activate(connection, contract, run_id)


def rollback(connection: psycopg.Connection, contract: Contract) -> Publication:
    """Return to the publication that preceded the current one (stack semantics)."""

    with connection.transaction():
        current = _current(connection, lock=True)
        if current is None:
            raise ActivationError("Nothing is active")
        previous_seq = _history(connection, current.seq)["previous_seq"]
        if previous_seq is None:
            raise ActivationError("The active release has no predecessor to roll back to")
        target = _history(connection, previous_seq)
        return _publish(
            connection,
            contract,
            target["run_id"],
            (target["jdf_load_id"], target["czptt_load_id"]),
            action="rollback",
            previous_seq=target["previous_seq"],
        )


def retained_loads(connection: psycopg.Connection, keep: int) -> set[int]:
    """The active loads, those of ``keep`` predecessors, and loads staged after the active ones."""

    current = _current(connection)
    if current is None:
        return {
            cast(int, load_id)
            for (load_id,) in connection.execute(
                "SELECT load_id FROM control.load WHERE status = 'loaded'"
            ).fetchall()
        }
    retained = set(current.load_ids)
    previous_seq = _history(connection, current.seq)["previous_seq"]
    predecessors: list[tuple[int, int]] = []
    while previous_seq is not None and len(predecessors) < keep:
        entry = _history(connection, previous_seq)
        loads = (cast(int, entry["jdf_load_id"]), cast(int, entry["czptt_load_id"]))
        if loads != current.load_ids and loads not in predecessors:
            predecessors.append(loads)
        previous_seq = entry["previous_seq"]
    for loads in predecessors:
        retained.update(loads)
    staged = connection.execute(
        "SELECT max(load_id) FROM control.load WHERE status = 'loaded' AND load_id > %s"
        " GROUP BY run_id, package",
        (max(current.load_ids),),
    ).fetchall()
    retained.update(cast(int, load_id) for (load_id,) in staged)
    return retained


def prune(
    connection: psycopg.Connection, contract: Contract, keep: int = DEFAULT_KEEP
) -> list[int]:
    """Drop the partitions of loads outside :func:`retained_loads`; metadata stays. Tables a
    failed load left behind (a process killed mid-load) go too; its status stays `failed`."""

    dropped: list[int] = []
    with connection.transaction():
        connection.execute("SELECT 1 FROM control.publication FOR UPDATE")
        for (failed,) in connection.execute(
            "SELECT load_id FROM control.load WHERE status = 'failed' ORDER BY load_id"
        ).fetchall():
            for table in static_tables(contract):
                connection.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(
                        partition_name(table.name, cast(int, failed))
                    )
                )
        retained = retained_loads(connection, keep)
        candidates = [
            cast(int, load_id)
            for (load_id,) in connection.execute(
                "SELECT load_id FROM control.load WHERE status = 'loaded' ORDER BY load_id"
            ).fetchall()
            if load_id not in retained
        ]
        if candidates:
            connection.execute(
                "DELETE FROM control.station_pairing_run"
                " WHERE jdf_load_id = ANY(%s) OR czptt_load_id = ANY(%s)",
                (candidates, candidates),
            )
        for load_id in candidates:
            for table in static_tables(contract):
                connection.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(partition_name(table.name, load_id))
                )
            connection.execute(
                "UPDATE control.load SET status = 'dropped', dropped_at = now() WHERE load_id = %s",
                (load_id,),
            )
            dropped.append(load_id)
    return dropped


def regroup(connection: psycopg.Connection, *, if_stale: bool = False) -> Publication | None:
    """Pair the active release's stations again (`obehy ref import station-pairs`): its
    overrides changed, its policy, or it was activated before pairing existed. With `if_stale`,
    only then (None when the pairing is current)."""

    current = _current(connection)
    if current is None:
        raise ActivationError("No release is active")
    if not pair(connection, current.load_ids, force=not if_stale):
        return None
    return current


def status(connection: psycopg.Connection) -> dict[str, Any]:
    with connection.cursor(row_factory=dict_row) as cursor:
        publication = cursor.execute(
            "SELECT run_id, jdf_load_id, czptt_load_id, activated_at FROM control.publication"
        ).fetchone()
        history = cursor.execute(
            "SELECT seq, run_id, jdf_load_id, czptt_load_id, action, previous_seq, activated_at"
            " FROM control.publication_history ORDER BY seq DESC LIMIT 10"
        ).fetchall()
        loads = cursor.execute(
            "SELECT load_id, run_id, package, status, started_at, finished_at, row_counts,"
            " warnings, error FROM control.load ORDER BY load_id DESC LIMIT 20"
        ).fetchall()
    pairing = (
        summary(connection, (publication["jdf_load_id"], publication["czptt_load_id"]))
        if publication and publication["jdf_load_id"] is not None
        else None
    )
    return {
        "publication": publication,
        "station_pairing": pairing,
        "history": history,
        "loads": loads,
    }
