from __future__ import annotations

from collections.abc import Hashable, Sequence
from datetime import date
from typing import TypeVar

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from obehy.persistence.builds import BuildService
from obehy.persistence.models import (
    IdentifierAliasRow,
    RailTripKeyRow,
    RoadTripKeyRow,
    SourceCallMapRow,
    SourceEntityMapRow,
    SourceTripMapRow,
)

T = TypeVar("T", bound=Hashable)


class AmbiguousStaticMappingError(RuntimeError):
    pass


class StaticMappingResolver:
    def __init__(
        self,
        session: Session,
        *,
        build_id: int | None = None,
        publication: str = "public",
    ) -> None:
        self.session = session
        self.build_id = build_id or BuildService(session).active_build_id(publication)

    def apply_alias(
        self, source_id: str, namespace: str, observed_id: str, service_date: date
    ) -> str:
        rows = self.session.scalars(
            select(IdentifierAliasRow).where(
                IdentifierAliasRow.build_id == self.build_id,
                IdentifierAliasRow.source_id == source_id,
                IdentifierAliasRow.namespace == namespace,
                IdentifierAliasRow.observed_id == observed_id,
                IdentifierAliasRow.valid_from <= service_date,
                or_(
                    IdentifierAliasRow.valid_to.is_(None),
                    IdentifierAliasRow.valid_to >= service_date,
                ),
            )
        ).all()
        if len(rows) > 1:
            raise AmbiguousStaticMappingError("Multiple aliases match the same dated identifier")
        return rows[0].canonical_value if rows else observed_id

    def source_trip(
        self,
        source_id: str,
        trip_namespace: str,
        source_trip_id: str,
        service_date: date,
        *,
        scheduled_start: int | None = None,
        scheduled_end: int | None = None,
        source_route_id: str | None = None,
        source_direction_id: str | None = None,
        source_start_location_id: str | None = None,
        source_end_location_id: str | None = None,
        source_block_id: str | None = None,
        source_run_id: str | None = None,
        source_duty_id: str | None = None,
        call_pattern_sha256: str | None = None,
    ) -> str | None:
        statement = select(SourceTripMapRow.trip_id).where(
            SourceTripMapRow.build_id == self.build_id,
            SourceTripMapRow.source_id == source_id,
            SourceTripMapRow.trip_namespace == trip_namespace,
            SourceTripMapRow.source_trip_id == source_trip_id,
            SourceTripMapRow.valid_from <= service_date,
            SourceTripMapRow.valid_to >= service_date,
        )
        if scheduled_start is not None:
            statement = statement.where(SourceTripMapRow.scheduled_start == scheduled_start)
        optional_context = (
            (SourceTripMapRow.scheduled_end, scheduled_end),
            (SourceTripMapRow.source_route_id, source_route_id),
            (SourceTripMapRow.source_direction_id, source_direction_id),
            (SourceTripMapRow.source_start_location_id, source_start_location_id),
            (SourceTripMapRow.source_end_location_id, source_end_location_id),
            (SourceTripMapRow.source_block_id, source_block_id),
            (SourceTripMapRow.source_run_id, source_run_id),
            (SourceTripMapRow.source_duty_id, source_duty_id),
            (SourceTripMapRow.call_pattern_sha256, call_pattern_sha256),
        )
        for column, value in optional_context:
            if value is not None:
                statement = statement.where(or_(column.is_(None), column == value))
        return self._one(self.session.scalars(statement).all(), "source trip")

    def source_entity(
        self,
        source_id: str,
        identifier_namespace: str,
        entity_kind: str,
        source_object_id: str,
    ) -> str | None:
        values = self.session.scalars(
            select(SourceEntityMapRow.public_id).where(
                SourceEntityMapRow.build_id == self.build_id,
                SourceEntityMapRow.source_id == source_id,
                SourceEntityMapRow.identifier_namespace == identifier_namespace,
                SourceEntityMapRow.entity_kind == entity_kind,
                SourceEntityMapRow.source_object_id == source_object_id,
            )
        ).all()
        return self._one(values, "source entity")

    def source_call(
        self,
        source_id: str,
        trip_namespace: str,
        source_trip_id: str,
        call_namespace: str,
        source_sequence: str,
        service_date: date,
        *,
        scheduled_start: int | None = None,
    ) -> tuple[str, int] | None:
        trip_id = self.source_trip(
            source_id,
            trip_namespace,
            source_trip_id,
            service_date,
            scheduled_start=scheduled_start,
        )
        if trip_id is None:
            return None
        values = (
            self.session.execute(
                select(SourceCallMapRow.trip_id, SourceCallMapRow.call_sequence).where(
                    SourceCallMapRow.build_id == self.build_id,
                    SourceCallMapRow.source_id == source_id,
                    SourceCallMapRow.trip_namespace == trip_namespace,
                    SourceCallMapRow.source_trip_id == source_trip_id,
                    SourceCallMapRow.call_namespace == call_namespace,
                    SourceCallMapRow.source_sequence == source_sequence,
                    SourceCallMapRow.trip_id == trip_id,
                )
            )
            .tuples()
            .all()
        )
        return self._one(values, "source call")

    def road_trip(self, cis_line_id: str, cis_trip_id: int, service_date: date) -> str | None:
        values = self.session.scalars(
            select(RoadTripKeyRow.trip_id).where(
                RoadTripKeyRow.build_id == self.build_id,
                RoadTripKeyRow.cis_line_id == cis_line_id,
                RoadTripKeyRow.cis_trip_id == cis_trip_id,
                RoadTripKeyRow.valid_from <= service_date,
                RoadTripKeyRow.valid_to >= service_date,
            )
        ).all()
        return self._one(values, "CIS road trip")

    def rail_trip(self, train_number: int, service_date: date) -> str | None:
        values = self.session.scalars(
            select(RailTripKeyRow.trip_id).where(
                RailTripKeyRow.build_id == self.build_id,
                RailTripKeyRow.train_number == train_number,
                RailTripKeyRow.valid_from <= service_date,
                RailTripKeyRow.valid_to >= service_date,
            )
        ).all()
        return self._one(values, "train number")

    @staticmethod
    def _one(values: Sequence[T], label: str) -> T | None:
        unique = set(values)
        if len(unique) > 1:
            raise AmbiguousStaticMappingError(f"Ambiguous {label} mapping")
        return next(iter(unique)) if unique else None
