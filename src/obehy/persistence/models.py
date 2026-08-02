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
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Sequence,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import DATERANGE, JSONB, ExcludeConstraint
from sqlalchemy.dialects.postgresql.ranges import Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from obehy.domain.identifiers import EntityKind


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[str]: JSONB}


ID_SEQUENCES: dict[EntityKind, Sequence] = {
    kind: Sequence(f"canonical_{kind.value}_seq", start=1, maxvalue=999_999_999)
    for kind in EntityKind
}

ENTITY_KINDS = tuple(kind.value for kind in EntityKind)
ENTITY_KIND_SQL = ",".join(repr(value) for value in ENTITY_KINDS)
DOMAIN_SQL = "'surface','heavy_rail'"
BUILD_STATE_SQL = "'building','ready','active','retired','failed','pruning','pruned'"
REVIEW_STATE_SQL = "'unresolved','ambiguous','accepted','manually_resolved'"


class CanonicalEntityRow(Base):
    __tablename__ = "canonical_entity"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    redirect_to_id: Mapped[str | None] = mapped_column(
        ForeignKey("canonical_entity.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(f"kind IN ({ENTITY_KIND_SQL})", name="ck_canonical_entity_kind"),
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


class OperatorRow(Base):
    __tablename__ = "operator"
    id: Mapped[str] = mapped_column(ForeignKey("canonical_entity.id"), primary_key=True)


class StopPlaceRow(Base):
    __tablename__ = "stop_place"
    id: Mapped[str] = mapped_column(ForeignKey("canonical_entity.id"), primary_key=True)
    location_domain: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        CheckConstraint(f"location_domain IN ({DOMAIN_SQL})", name="ck_stop_place_domain"),
    )


class BoardingPointRow(Base):
    __tablename__ = "boarding_point"
    id: Mapped[str] = mapped_column(ForeignKey("canonical_entity.id"), primary_key=True)
    stop_place_id: Mapped[str] = mapped_column(ForeignKey("stop_place.id"), nullable=False)
    is_unspecified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    id: Mapped[str] = mapped_column(ForeignKey("canonical_entity.id"), primary_key=True)
    location_domain: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        CheckConstraint(f"location_domain IN ({DOMAIN_SQL})", name="ck_operational_point_domain"),
    )


class RouteRow(Base):
    __tablename__ = "canonical_route"
    id: Mapped[str] = mapped_column(ForeignKey("canonical_entity.id"), primary_key=True)
    location_domain: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        CheckConstraint(f"location_domain IN ({DOMAIN_SQL})", name="ck_route_domain"),
    )


class ScheduledTripRow(Base):
    __tablename__ = "scheduled_trip"
    id: Mapped[str] = mapped_column(ForeignKey("canonical_entity.id"), primary_key=True)
    location_domain: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (CheckConstraint(f"location_domain IN ({DOMAIN_SQL})", name="ck_trip_domain"),)


class SourceRow(Base):
    __tablename__ = "source"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    default_domain: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Prague")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        CheckConstraint(
            f"default_domain IS NULL OR default_domain IN ({DOMAIN_SQL})", name="ck_source_domain"
        ),
    )


class SourceSnapshotRow(Base):
    __tablename__ = "source_snapshot"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    declared_version: Mapped[str | None] = mapped_column(Text)
    artifact_key: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    __table_args__ = (
        UniqueConstraint("source_id", "content_sha256", name="uq_source_snapshot_content"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_snapshot_sha256"),
        CheckConstraint("artifact_key !~ '^(?:[A-Za-z]:|/|\\\\)'", name="ck_snapshot_relative_key"),
    )


class SourceSnapshotArtifactRow(Base):
    __tablename__ = "source_snapshot_artifact"
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("source_snapshot.id", ondelete="CASCADE"), primary_key=True
    )
    logical_role: Mapped[str] = mapped_column(String(96), primary_key=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_artifact_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_size"),
        CheckConstraint("storage_key !~ '^(?:[A-Za-z]:|/|\\\\)'", name="ck_artifact_relative_key"),
    )


class SourceObjectRow(Base):
    __tablename__ = "source_object"
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("source_snapshot.id", ondelete="CASCADE"), primary_key=True
    )
    entity_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_object_id: Mapped[str] = mapped_column(Text, primary_key=True)
    location_domain: Mapped[str | None] = mapped_column(String(16))
    record_locator: Mapped[str] = mapped_column(Text, nullable=False)
    natural_key_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint(f"entity_kind IN ({ENTITY_KIND_SQL})", name="ck_source_object_kind"),
        CheckConstraint(
            f"location_domain IS NULL OR location_domain IN ({DOMAIN_SQL})",
            name="ck_source_object_domain",
        ),
    )


class SourceBindingRow(Base):
    __tablename__ = "source_binding"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    location_domain: Mapped[str | None] = mapped_column(String(16))
    canonical_entity_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id"), nullable=False
    )
    validity: Mapped[Range[date]] = mapped_column(DATERANGE, nullable=False)
    match_method: Mapped[str] = mapped_column(String(64), nullable=False)
    match_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    review_state: Mapped[str] = mapped_column(String(24), nullable=False, default="accepted")
    first_seen_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    last_seen_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        CheckConstraint("NOT isempty(validity)", name="ck_source_binding_nonempty"),
        CheckConstraint("match_confidence BETWEEN 0 AND 1", name="ck_source_binding_confidence"),
        CheckConstraint(f"entity_kind IN ({ENTITY_KIND_SQL})", name="ck_source_binding_kind"),
        CheckConstraint(
            f"location_domain IS NULL OR location_domain IN ({DOMAIN_SQL})",
            name="ck_source_binding_domain",
        ),
        CheckConstraint(f"review_state IN ({REVIEW_STATE_SQL})", name="ck_binding_review_state"),
        ExcludeConstraint(
            ("source_id", "="),
            ("entity_kind", "="),
            ("source_object_id", "="),
            ("validity", "&&"),
            name="ex_source_binding_no_overlap",
            using="gist",
        ),
    )


class ExternalIdentifierRow(Base):
    __tablename__ = "external_identifier"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_entity_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_entity.id"), nullable=False
    )
    validity: Mapped[Range[date]] = mapped_column(DATERANGE, nullable=False)
    asserting_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        Index("ix_external_identifier_lookup", "namespace", "value"),
        CheckConstraint("NOT isempty(validity)", name="ck_external_identifier_nonempty"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_external_identifier_confidence"),
    )


class IdentifierAliasRow(Base):
    __tablename__ = "identifier_alias"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    identifier_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    validity: Mapped[Range[date]] = mapped_column(DATERANGE, nullable=False)
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
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    error_category: Mapped[str] = mapped_column(String(64), nullable=False)
    review_state: Mapped[str] = mapped_column(String(24), nullable=False, default="unresolved")
    candidate_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        CheckConstraint(f"review_state IN ({REVIEW_STATE_SQL})", name="ck_diagnostic_review_state"),
    )


class CanonicalRoadRouteKeyRow(Base):
    __tablename__ = "canonical_road_route_key"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cis_line_id: Mapped[str] = mapped_column(String(6), nullable=False)
    route_id: Mapped[str] = mapped_column(ForeignKey("canonical_route.id"), nullable=False)
    validity: Mapped[Range[date]] = mapped_column(DATERANGE, nullable=False)
    asserting_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    __table_args__ = (
        Index("ix_road_route_cis_line", "cis_line_id"),
        CheckConstraint("cis_line_id ~ '^[0-9]{6}$'", name="ck_road_route_cis_line"),
        CheckConstraint("NOT isempty(validity)", name="ck_road_route_key_nonempty"),
    )


class CanonicalRoadTripKeyRow(Base):
    __tablename__ = "canonical_road_trip_key"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cis_line_id: Mapped[str] = mapped_column(String(6), nullable=False)
    cis_trip_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trip_id: Mapped[str] = mapped_column(ForeignKey("scheduled_trip.id"), nullable=False)
    validity: Mapped[Range[date]] = mapped_column(DATERANGE, nullable=False)
    asserting_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    __table_args__ = (
        Index("ix_road_trip_cis_pair", "cis_line_id", "cis_trip_id"),
        CheckConstraint("cis_line_id ~ '^[0-9]{6}$'", name="ck_road_trip_cis_line"),
        CheckConstraint("cis_trip_id >= 0", name="ck_road_trip_cis_trip"),
        CheckConstraint("NOT isempty(validity)", name="ck_road_trip_key_nonempty"),
    )


class CanonicalRailTripKeyRow(Base):
    __tablename__ = "canonical_rail_trip_key"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    train_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trip_id: Mapped[str] = mapped_column(ForeignKey("scheduled_trip.id"), nullable=False)
    validity: Mapped[Range[date]] = mapped_column(DATERANGE, nullable=False)
    asserting_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    __table_args__ = (
        Index("ix_rail_trip_train_number", "train_number"),
        CheckConstraint("train_number > 0", name="ck_rail_trip_train_number"),
        CheckConstraint("NOT isempty(validity)", name="ck_rail_trip_key_nonempty"),
    )


class StaticBuildRow(Base):
    __tablename__ = "static_build"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(160), nullable=False)
    output_artifact_key: Mapped[str | None] = mapped_column(Text)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(f"state IN ({BUILD_STATE_SQL})", name="ck_static_build_state"),
        CheckConstraint("config_sha256 ~ '^[0-9a-f]{64}$'", name="ck_build_config_sha256"),
    )


class StaticBuildInputRow(Base):
    __tablename__ = "static_build_input"
    build_id: Mapped[int] = mapped_column(
        ForeignKey("static_build.id", ondelete="CASCADE"), primary_key=True
    )
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshot.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(96), primary_key=True)


class PublicationRow(Base):
    __tablename__ = "publication"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    active_build_id: Mapped[int] = mapped_column(ForeignKey("static_build.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BuildValidationRow(Base):
    __tablename__ = "build_validation"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("static_build.id"), nullable=False)
    validator: Mapped[str] = mapped_column(String(96), nullable=False)
    is_advisory: Mapped[bool] = mapped_column(Boolean, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BuildDiagnosticRow(Base):
    __tablename__ = "build_diagnostic"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("static_build.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(96), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    blocks_activation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info','warning','error')", name="ck_build_diagnostic_severity"
        ),
    )


class RevisionMixin:
    build_id: Mapped[int] = mapped_column(
        ForeignKey("static_build.id", ondelete="CASCADE"), primary_key=True
    )
    base_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    base_object_kind: Mapped[str | None] = mapped_column(String(32))
    base_object_id: Mapped[str | None] = mapped_column(Text)
    extensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class OperatorRevisionRow(RevisionMixin, Base):
    __tablename__ = "operator_revision"
    operator_id: Mapped[str] = mapped_column(ForeignKey("operator.id"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(Text)
    fare_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)


class StopPlaceRevisionRow(RevisionMixin, Base):
    __tablename__ = "stop_place_revision"
    stop_place_id: Mapped[str] = mapped_column(ForeignKey("stop_place.id"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    public_code: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False)
    )
    url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(64))
    wheelchair_boarding: Mapped[int | None] = mapped_column(SmallInteger)


class BoardingPointRevisionRow(RevisionMixin, Base):
    __tablename__ = "boarding_point_revision"
    boarding_point_id: Mapped[str] = mapped_column(
        ForeignKey("boarding_point.id"), primary_key=True
    )
    name: Mapped[str | None] = mapped_column(Text)
    public_code: Mapped[str | None] = mapped_column(Text)
    position: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False)
    )
    wheelchair_boarding: Mapped[int | None] = mapped_column(SmallInteger)


class OperationalPointRevisionRow(RevisionMixin, Base):
    __tablename__ = "operational_point_revision"
    operational_point_id: Mapped[str] = mapped_column(
        ForeignKey("operational_point.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    public_code: Mapped[str | None] = mapped_column(Text)
    position: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False)
    )


class RouteRevisionRow(RevisionMixin, Base):
    __tablename__ = "route_revision"
    route_id: Mapped[str] = mapped_column(ForeignKey("canonical_route.id"), primary_key=True)
    operator_id: Mapped[str] = mapped_column(ForeignKey("operator.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    gtfs_route_type: Mapped[int] = mapped_column(Integer, nullable=False)
    short_name: Mapped[str | None] = mapped_column(Text)
    long_name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(String(6))
    text_color: Mapped[str | None] = mapped_column(String(6))
    sort_order: Mapped[int | None] = mapped_column(Integer)


class ServiceCalendarRow(Base):
    __tablename__ = "service_calendar"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(
        ForeignKey("static_build.id", ondelete="CASCADE"), nullable=False
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    weekday_mask: Mapped[int] = mapped_column(Integer, nullable=False)
    source_service_id: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("build_id", "id", name="uq_calendar_build_id"),
        CheckConstraint("valid_to >= valid_from", name="ck_calendar_validity"),
        CheckConstraint("weekday_mask BETWEEN 0 AND 127", name="ck_weekday_mask"),
    )


class ServiceExceptionRow(Base):
    __tablename__ = "service_exception"
    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("service_calendar.id", ondelete="CASCADE"), primary_key=True
    )
    service_date: Mapped[date] = mapped_column(Date, primary_key=True)
    added: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ShapeRow(RevisionMixin, Base):
    __tablename__ = "shape"
    shape_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    generation_method: Mapped[str] = mapped_column(String(32), nullable=False, default="source")


class ShapePointRow(Base):
    __tablename__ = "shape_point"
    build_id: Mapped[int] = mapped_column(primary_key=True)
    shape_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    position: Mapped[WKBElement] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    distance_traveled: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "shape_id"], ["shape.build_id", "shape.shape_id"], ondelete="CASCADE"
        ),
        CheckConstraint("sequence >= 0", name="ck_shape_point_sequence"),
        CheckConstraint(
            "distance_traveled IS NULL OR distance_traveled >= 0", name="ck_shape_point_distance"
        ),
    )


class TripRevisionRow(RevisionMixin, Base):
    __tablename__ = "trip_revision"
    trip_id: Mapped[str] = mapped_column(ForeignKey("scheduled_trip.id"), primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("canonical_route.id"), nullable=False)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("service_calendar.id"), nullable=False)
    direction: Mapped[int | None] = mapped_column(SmallInteger)
    headsign: Mapped[str | None] = mapped_column(Text)
    short_name: Mapped[str | None] = mapped_column(Text)
    block_key: Mapped[str | None] = mapped_column(Text)
    wheelchair_accessible: Mapped[int | None] = mapped_column(SmallInteger)
    bikes_allowed: Mapped[int | None] = mapped_column(SmallInteger)
    shape_id: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        ForeignKeyConstraint(["build_id", "shape_id"], ["shape.build_id", "shape.shape_id"]),
        CheckConstraint("direction IS NULL OR direction IN (0,1)", name="ck_trip_direction"),
    )


class TripCallRevisionRow(Base):
    __tablename__ = "trip_call_revision"
    build_id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("canonical_entity.id"), nullable=False)
    passenger_service: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scheduled_boarding_point_id: Mapped[str | None] = mapped_column(ForeignKey("boarding_point.id"))
    scheduled_arrival: Mapped[int | None] = mapped_column(Integer)
    scheduled_departure: Mapped[int | None] = mapped_column(Integer)
    scheduled_passage: Mapped[int | None] = mapped_column(Integer)
    pickup_type: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    dropoff_type: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    timepoint: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stop_headsign: Mapped[str | None] = mapped_column(Text)
    shape_distance_traveled: Mapped[float | None] = mapped_column(Float)
    base_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id"))
    base_object_kind: Mapped[str | None] = mapped_column(String(32))
    base_object_id: Mapped[str | None] = mapped_column(Text)
    extensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id"],
            ["trip_revision.build_id", "trip_revision.trip_id"],
            ondelete="CASCADE",
        ),
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
        CheckConstraint("pickup_type BETWEEN 0 AND 3", name="ck_call_pickup"),
        CheckConstraint("dropoff_type BETWEEN 0 AND 3", name="ck_call_dropoff"),
        CheckConstraint(
            "(passenger_service AND scheduled_boarding_point_id IS NOT NULL "
            "AND scheduled_passage IS NULL) OR (NOT passenger_service "
            "AND scheduled_boarding_point_id IS NULL AND scheduled_arrival IS NULL "
            "AND scheduled_departure IS NULL AND pickup_type = 1 AND dropoff_type = 1)",
            name="ck_trip_call_shape",
        ),
    )


class TransferRevisionRow(Base):
    __tablename__ = "transfer_revision"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(
        ForeignKey("static_build.id", ondelete="CASCADE"), nullable=False
    )
    from_boarding_point_id: Mapped[str] = mapped_column(
        ForeignKey("boarding_point.id"), nullable=False
    )
    to_boarding_point_id: Mapped[str] = mapped_column(
        ForeignKey("boarding_point.id"), nullable=False
    )
    from_route_id: Mapped[str | None] = mapped_column(ForeignKey("canonical_route.id"))
    to_route_id: Mapped[str | None] = mapped_column(ForeignKey("canonical_route.id"))
    from_trip_id: Mapped[str | None] = mapped_column(ForeignKey("scheduled_trip.id"))
    to_trip_id: Mapped[str | None] = mapped_column(ForeignKey("scheduled_trip.id"))
    transfer_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    minimum_transfer_time: Mapped[int | None] = mapped_column(Integer)


class FareSystemRow(Base):
    __tablename__ = "fare_system"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class FareZoneRevisionRow(Base):
    __tablename__ = "fare_zone_revision"
    build_id: Mapped[int] = mapped_column(
        ForeignKey("static_build.id", ondelete="CASCADE"), primary_key=True
    )
    fare_system_id: Mapped[int] = mapped_column(ForeignKey("fare_system.id"), primary_key=True)
    zone_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    extensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class StopZoneAssignmentRow(Base):
    __tablename__ = "stop_zone_assignment"
    build_id: Mapped[int] = mapped_column(primary_key=True)
    stop_place_id: Mapped[str] = mapped_column(ForeignKey("stop_place.id"), primary_key=True)
    fare_system_id: Mapped[int] = mapped_column(primary_key=True)
    zone_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "fare_system_id", "zone_key"],
            [
                "fare_zone_revision.build_id",
                "fare_zone_revision.fare_system_id",
                "fare_zone_revision.zone_key",
            ],
            ondelete="CASCADE",
        ),
    )


class TripCallZoneAssignmentRow(Base):
    __tablename__ = "trip_call_zone_assignment"
    build_id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    fare_system_id: Mapped[int] = mapped_column(primary_key=True)
    zone_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id", "sequence"],
            [
                "trip_call_revision.build_id",
                "trip_call_revision.trip_id",
                "trip_call_revision.sequence",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["build_id", "fare_system_id", "zone_key"],
            [
                "fare_zone_revision.build_id",
                "fare_zone_revision.fare_system_id",
                "fare_zone_revision.zone_key",
            ],
            ondelete="CASCADE",
        ),
    )


class SelectedFieldProvenanceRow(Base):
    __tablename__ = "selected_field_provenance"
    build_id: Mapped[int] = mapped_column(
        ForeignKey("static_build.id", ondelete="CASCADE"), primary_key=True
    )
    object_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_key: Mapped[str] = mapped_column(Text, primary_key=True)
    field_name: Mapped[str] = mapped_column(String(96), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshot.id"), nullable=False)
    source_object_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    selection_rule: Mapped[str] = mapped_column(String(160), nullable=False)


class TripInstanceRow(Base):
    __tablename__ = "trip_instance"
    trip_id: Mapped[str] = mapped_column(ForeignKey("scheduled_trip.id"), primary_key=True)
    operating_date: Mapped[date] = mapped_column(Date, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
