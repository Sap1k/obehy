from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, ExcludeConstraint
from sqlalchemy.dialects.postgresql.ranges import Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from obehy.domain.identifiers import EntityKind


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


ID_SEQUENCES: dict[EntityKind, Sequence] = {
    EntityKind.STOP_PLACE: Sequence("canonical_stop_place_seq", start=1, maxvalue=999_999_999),
    EntityKind.BOARDING_POINT: Sequence(
        "canonical_boarding_point_seq", start=1, maxvalue=999_999_999
    ),
    EntityKind.OPERATIONAL_POINT: Sequence(
        "canonical_operational_point_seq", start=1, maxvalue=999_999_999
    ),
    EntityKind.ROUTE: Sequence("canonical_route_seq", start=1, maxvalue=999_999_999),
    EntityKind.SCHEDULED_TRIP: Sequence(
        "canonical_scheduled_trip_seq", start=1, maxvalue=999_999_999
    ),
}


class CanonicalEntityRow(Base):
    __tablename__ = "canonical_entity"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    redirect_to_id: Mapped[str | None] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('stop_place','boarding_point','operational_point','route','scheduled_trip')",
            name="ck_canonical_entity_kind",
        ),
        CheckConstraint(
            "status IN ('active','tombstoned','redirected')", name="ck_canonical_entity_status"
        ),
        CheckConstraint(
            "(status = 'redirected' AND redirect_to_id IS NOT NULL) OR "
            "(status <> 'redirected' AND redirect_to_id IS NULL)",
            name="ck_canonical_entity_redirect_state",
        ),
        CheckConstraint(
            "redirect_to_id IS NULL OR redirect_to_id <> id", name="ck_no_self_redirect"
        ),
    )


class SourceRow(Base):
    __tablename__ = "source"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Prague")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SourceBindingRow(Base):
    __tablename__ = "source_binding"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_entity_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), nullable=False
    )
    validity: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    match_confidence: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(160))

    __table_args__ = (
        CheckConstraint("NOT isempty(validity)", name="ck_source_binding_nonempty"),
        CheckConstraint(
            "match_confidence >= 0 AND match_confidence <= 1",
            name="ck_source_binding_confidence",
        ),
        ExcludeConstraint(
            ("source_id", "="),
            ("entity_kind", "="),
            ("source_object_id", "="),
            ("validity", "&&"),
            name="ex_source_binding_no_overlap",
            using="gist",
        ),
    )


class IdentifierAliasRow(Base):
    __tablename__ = "identifier_alias"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    identifier_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    validity: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("NOT isempty(validity)", name="ck_identifier_alias_nonempty"),
        ExcludeConstraint(
            ("source_id", "="),
            ("identifier_kind", "="),
            ("observed_value", "="),
            ("validity", "&&"),
            name="ex_identifier_alias_no_overlap",
            using="gist",
        ),
    )


class IdentityDiagnosticRow(Base):
    __tablename__ = "identity_diagnostic"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_category: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class StopPlaceRow(Base):
    __tablename__ = "stop_place"

    id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    centroid: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )


class BoardingPointRow(Base):
    __tablename__ = "boarding_point"

    id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), primary_key=True
    )
    stop_place_id: Mapped[str] = mapped_column(
        ForeignKey("stop_place.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(Text)
    source_code: Mapped[str | None] = mapped_column(Text)
    is_unspecified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_boarding_point_unspecified_per_place",
            "stop_place_id",
            unique=True,
            postgresql_where=text("is_unspecified"),
        ),
    )


class OperationalPointRow(Base):
    __tablename__ = "operational_point"

    id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(Text)
    position: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )


class RouteRow(Base):
    __tablename__ = "canonical_route"

    id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    cis_line_id: Mapped[str | None] = mapped_column(String(6))
    public_name: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "cis_line_id IS NULL OR cis_line_id ~ '^[0-9]{6}$'", name="ck_route_cis_line_id"
        ),
        UniqueConstraint("cis_line_id", name="uq_route_cis_line_id"),
    )


class ServiceCalendarRow(Base):
    __tablename__ = "service_calendar"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    weekday_mask: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("valid_to >= valid_from", name="ck_calendar_validity"),
        CheckConstraint("weekday_mask >= 0 AND weekday_mask <= 127", name="ck_weekday_mask"),
    )


class ServiceExceptionRow(Base):
    __tablename__ = "service_exception"

    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("service_calendar.id", ondelete="CASCADE"), primary_key=True
    )
    service_date: Mapped[date] = mapped_column(Date, primary_key=True)
    added: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ScheduledTripRow(Base):
    __tablename__ = "scheduled_trip"

    id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), primary_key=True
    )
    route_id: Mapped[str] = mapped_column(ForeignKey("canonical_route.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("service_calendar.id"), nullable=False)
    timetable_variant: Mapped[str] = mapped_column(Text, nullable=False)
    cis_line_id: Mapped[str | None] = mapped_column(String(6))
    cis_trip_id: Mapped[int | None] = mapped_column(BigInteger)
    train_number: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("direction IN (0, 1)", name="ck_scheduled_trip_direction"),
        CheckConstraint(
            "((cis_line_id IS NOT NULL AND cis_trip_id IS NOT NULL AND train_number IS NULL) OR "
            "(cis_line_id IS NULL AND cis_trip_id IS NULL AND train_number IS NOT NULL))",
            name="ck_scheduled_trip_identity",
        ),
        CheckConstraint(
            "cis_line_id IS NULL OR cis_line_id ~ '^[0-9]{6}$'", name="ck_trip_cis_line_id"
        ),
        CheckConstraint("cis_trip_id IS NULL OR cis_trip_id >= 0", name="ck_trip_cis_trip_id"),
        CheckConstraint("train_number IS NULL OR train_number > 0", name="ck_trip_train_number"),
    )


class TripCallRow(Base):
    __tablename__ = "trip_call"

    trip_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_trip.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT"), nullable=False
    )
    passenger_service: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scheduled_boarding_point_id: Mapped[str | None] = mapped_column(
        ForeignKey("boarding_point.id", ondelete="RESTRICT")
    )
    scheduled_arrival: Mapped[int | None] = mapped_column(Integer)
    scheduled_departure: Mapped[int | None] = mapped_column(Integer)
    scheduled_passage: Mapped[int | None] = mapped_column(Integer)
    pickup_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dropoff_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_trip_call_sequence"),
        CheckConstraint(
            "scheduled_arrival IS NULL OR scheduled_arrival >= 0", name="ck_call_arrival"
        ),
        CheckConstraint(
            "scheduled_departure IS NULL OR scheduled_departure >= 0", name="ck_call_departure"
        ),
        CheckConstraint(
            "scheduled_passage IS NULL OR scheduled_passage >= 0", name="ck_call_passage"
        ),
        CheckConstraint(
            "(passenger_service AND scheduled_boarding_point_id IS NOT NULL "
            "AND scheduled_passage IS NULL "
            "AND (scheduled_arrival IS NOT NULL OR scheduled_departure IS NOT NULL)) OR "
            "(NOT passenger_service AND scheduled_boarding_point_id IS NULL "
            "AND scheduled_arrival IS NULL AND scheduled_departure IS NULL "
            "AND scheduled_passage IS NOT NULL AND NOT pickup_allowed AND NOT dropoff_allowed)",
            name="ck_trip_call_shape",
        ),
    )
