"""The rows the writer (`emit/db.py`) sends, and where they go: a `Store`.

`Writer` turns effects and core state into rows; a store writes them. `SqlStore`
(`emit/sql_store.py`) is PostgreSQL; `obehy rt compare` records the same rows instead
(`emit/recording.py`), so the harness sees exactly what the database would be sent.

Rows are tuples in the order of their `*_COLUMNS`. Strings history keeps by number
(`history.term`) and the writer's `derivation_id` are the SQL store's to add: a row names a term
by its text and carries no derivation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, LiteralString, Protocol

from obehy.realtime.model import Derivation, SnapshotJourney

if TYPE_CHECKING:
    from obehy.realtime.emit.vehicles import VehicleBook, VehicleRows

Row = tuple[Any, ...]

# `facts` is the fact JSON (`model_json.facts_to_json`) as a dict; SQL sends it as text.
OBSERVATION_COLUMNS = (
    "received_at",
    "source",
    "channel",
    "feed",
    "raw_sha256",
    "raw_item",
    "observed_at",
    "decoder_version",
    "fact_schema_version",
    "facts",
    "journey_namespace",
    "journey_key",
    "service_date",
    "reason",
    "release_id",
    "vehicle_id",
    "state_since",
)
# `location`, `method` and `source` are terms; `event_time` is the interval's middle.
EVENT_COLUMNS = (
    "feed",
    "key_namespace",
    "key",
    "service_date",
    "location",
    "visit_n",
    "event_type",
    "event_time",
    "interval_lo",
    "interval_hi",
    "method",
    "source",
)
ASSIGNMENT_COLUMNS = (
    "feed",
    "source",
    "source_vehicle_id",
    "key_namespace",
    "key",
    "service_date",
    "first_seen",
    "last_seen",
    "method",
    "vehicle_id",
)
LINK_COLUMNS = (
    "feed",
    "source_namespace",
    "source_key",
    "target_namespace",
    "target_key",
    "service_date",
    "kind",
)
PLATFORM_COLUMNS = (
    "feed",
    "key_namespace",
    "key",
    "service_date",
    "location_id",
    "visit_n",
    "event_type",
    "value",
    "label",
    "boarding_point_id",
    "source",
    "first_seen",
    "last_seen",
)
# `cancelled` false: seen running after all (restores the journey).
CANCELLATION_COLUMNS = (
    "feed",
    "key_namespace",
    "key",
    "service_date",
    "source",
    "at",
    "cancelled",
)


@dataclass(frozen=True, slots=True)
class StateTable:
    """A current-state table of `rt`: its columns, key and the key's SQL types."""

    name_text: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    key_types: tuple[LiteralString, ...]


VEHICLES = StateTable(
    "vehicle_state_current",
    (
        "feed",
        "source",
        "source_vehicle_id",
        "status",
        "last_seen",
        "key_namespace",
        "key",
        "service_date",
        "trip_id",
        "latitude",
        "longitude",
        "bearing",
        "reason",
        "release_id",
        "vehicle_id",
        "bound_by",
        "represents",
        "position_at",
    ),
    ("feed", "source", "source_vehicle_id"),
    ("text", "text", "text"),
)
# `calls` and `sources` are JSON text (jsonb columns).
TRIPS = StateTable(
    "trip_state_current",
    (
        "feed",
        "key_namespace",
        "key",
        "service_date",
        "release_id",
        "trip_id",
        "lifecycle",
        "delay_s",
        "off_route",
        "stale",
        "updated_at",
        "calls",
        "sources",
    ),
    ("feed", "key_namespace", "key", "service_date"),
    ("text", "text", "text", "date"),
)
# The feed trips each journey publishes over its calls (a run's parts, a road trip itself), so
# readers find a trip's state by its feed trip ID (BASE_PLAN.md section 30).
PARTS = StateTable(
    "trip_part_current",
    (
        "feed",
        "key_namespace",
        "key",
        "service_date",
        "trip_id",
        "first_call",
        "last_call",
        "release_id",
    ),
    ("feed", "key_namespace", "key", "service_date", "trip_id"),
    ("text", "text", "text", "date", "text"),
)


class Store(Protocol):
    """Where the writer's rows go. Each `Writer.write` is one `transaction`; `prepare` runs
    before it, outside."""

    def open(self, derivation: Derivation) -> VehicleBook:
        """Start writing under `derivation`; the vehicle book as stored."""
        ...

    def reload_registry(self, book: VehicleBook) -> None: ...

    def prepare(self, hours: set[datetime], days: set[date], terms: Iterable[str]) -> None:
        """Observation hours and history service dates about to be written, and the terms."""
        ...

    def transaction(self) -> AbstractContextManager[object]: ...

    def observations(self, rows: Sequence[Row]) -> None: ...

    def snapshot(self, snapshot: SnapshotJourney) -> None:
        """A journey's schedule: a new revision unless the stored one has its release and trip."""
        ...

    def events(self, rows: Sequence[Row]) -> None: ...

    def assignments(self, rows: Sequence[Row]) -> None: ...

    def links(self, rows: Sequence[Row]) -> None: ...

    def platforms(self, rows: Sequence[Row]) -> None: ...

    def cancellations(self, rows: Sequence[Row]) -> None: ...

    def vehicles(self, rows: VehicleRows) -> None:
        """Live attributes, pairings and profiles (`VehicleBook.rows`)."""
        ...

    def replace_state(self, table: StateTable, feed: str, rows: Sequence[Row]) -> None:
        """A feed's rows of `table`, replacing all it had."""
        ...

    def delete_state(self, table: StateTable, keys: Sequence[Row]) -> None: ...

    def upsert_state(self, table: StateTable, rows: Sequence[Row]) -> None: ...

    def clear_history(self, days: Sequence[date]) -> None: ...
