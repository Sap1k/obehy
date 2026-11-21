"""A `Store` that records the writer's rows instead of sending them (`obehy rt compare`).

Every row the SQL store (`emit/sql_store.py`) would be sent becomes a record in `pending`, in
the order the writer produced it; the harness takes them with `take`. The vehicle book starts as
on an empty database: no registry, no stored pairings or attributes. Partitions and terms are
storage details and are not recorded; transactions are not marked.

Record kinds (fields besides `kind`):

- `physical_vehicles`: `row` (source, mode, physical), once when the writer opens;
- `observation`: `row` (`rt.observation`; `facts` as the fact JSON object);
- `journey_schedule`: `snapshot` (the `SnapshotJourney`; SQL adds a revision unless the stored
  schedule has its release and trip);
- `stop_event`, `vehicle_assignment`, `journey_link`, `platform_evidence`,
  `journey_cancellation`: `row` (`emit/rows.py` columns; terms as text, no derivation);
- `vehicle_attribute`: `row`; `vehicle_pairing`: `op` (`delete` | `upsert`), `a`, `b`
  (`[source, source_vehicle_id]`) and with `upsert` `evidence`; `vehicle_profile_reset` (every
  stored profile deleted); `vehicle_profile`: `profile`;
- `state`: `table`, `op`: `replace` (with `feed`: the feed's rows deleted) followed by one
  `insert` per row (`row`), `delete` (`key`), `upsert` (`row`). JSON text columns (`calls`,
  `sources`) are parsed into objects.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from obehy.realtime.emit.rows import (
    ASSIGNMENT_COLUMNS,
    CANCELLATION_COLUMNS,
    EVENT_COLUMNS,
    LINK_COLUMNS,
    OBSERVATION_COLUMNS,
    PLATFORM_COLUMNS,
    Row,
    StateTable,
)
from obehy.realtime.emit.vehicles import VehicleBook, VehicleRows, physical_rows
from obehy.realtime.model import Derivation, SnapshotJourney
from obehy.reference.registry import Registry
from obehy.reference.vocabulary import load_vocabulary

Record = dict[str, Any]
JSON_TEXT = frozenset({"calls", "sources"})


def _named(columns: Sequence[str], row: Row) -> dict[str, Any]:
    return {
        name: json.loads(value) if name in JSON_TEXT and isinstance(value, str) else value
        for name, value in zip(columns, row, strict=True)
    }


def observation_record(row: Row) -> Record:
    """The `observation` record of an `rt.observation` row."""

    return {"kind": "observation", "row": _named(OBSERVATION_COLUMNS, row)}


@dataclass(slots=True)
class RecordingStore:
    pending: list[Record] = field(default_factory=list[Record])

    def take(self) -> list[Record]:
        taken, self.pending = self.pending, []
        return taken

    def _rows(self, kind: str, columns: Sequence[str], rows: Iterable[Row]) -> None:
        self.pending.extend({"kind": kind, "row": _named(columns, row)} for row in rows)

    def open(self, derivation: Derivation) -> VehicleBook:
        self._rows("physical_vehicles", ("source", "mode", "physical"), physical_rows())
        return VehicleBook(Registry(), load_vocabulary())

    def reload_registry(self, book: VehicleBook) -> None:
        pass

    def prepare(self, hours: set[datetime], days: set[date], terms: Iterable[str]) -> None:
        pass

    def transaction(self) -> AbstractContextManager[object]:
        return nullcontext()

    def observations(self, rows: Sequence[Row]) -> None:
        self.pending.extend(observation_record(row) for row in rows)

    def snapshot(self, snapshot: SnapshotJourney) -> None:
        self.pending.append({"kind": "journey_schedule", "snapshot": snapshot})

    def events(self, rows: Sequence[Row]) -> None:
        self._rows("stop_event", EVENT_COLUMNS, rows)

    def assignments(self, rows: Sequence[Row]) -> None:
        self._rows("vehicle_assignment", ASSIGNMENT_COLUMNS, rows)

    def links(self, rows: Sequence[Row]) -> None:
        self._rows("journey_link", LINK_COLUMNS, rows)

    def platforms(self, rows: Sequence[Row]) -> None:
        self._rows("platform_evidence", PLATFORM_COLUMNS, rows)

    def cancellations(self, rows: Sequence[Row]) -> None:
        self._rows("journey_cancellation", CANCELLATION_COLUMNS, rows)

    def vehicles(self, rows: VehicleRows) -> None:
        self._rows(
            "vehicle_attribute",
            ("source", "source_vehicle_id", "attribute", "value", "observed_at"),
            rows.attributes,
        )
        for a, b in rows.gone:
            self.pending.append({"kind": "vehicle_pairing", "op": "delete", "a": a, "b": b})
        for (a, b), evidence in rows.kept:
            self.pending.append(
                {"kind": "vehicle_pairing", "op": "upsert", "a": a, "b": b, "evidence": evidence}
            )
        if rows.rewrite:
            self.pending.append({"kind": "vehicle_profile_reset"})
        self.pending.extend({"kind": "vehicle_profile", "profile": p} for p in rows.profiles)

    def replace_state(self, table: StateTable, feed: str, rows: Sequence[Row]) -> None:
        name = table.name_text
        self.pending.append({"kind": "state", "table": name, "op": "replace", "feed": feed})
        self.pending.extend(
            {"kind": "state", "table": name, "op": "insert", "row": _named(table.columns, row)}
            for row in rows
        )

    def delete_state(self, table: StateTable, keys: Sequence[Row]) -> None:
        self.pending.extend(
            {"kind": "state", "table": table.name_text, "op": "delete", "key": _named(table.key, k)}
            for k in keys
        )

    def upsert_state(self, table: StateTable, rows: Sequence[Row]) -> None:
        self.pending.extend(
            {
                "kind": "state",
                "table": table.name_text,
                "op": "upsert",
                "row": _named(table.columns, row),
            }
            for row in rows
        )

    def clear_history(self, days: Sequence[date]) -> None:
        self.pending.append({"kind": "clear_history", "days": list(days)})
