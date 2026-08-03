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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[str]: JSONB}


SHA256_CHECK = "{column} ~ '^[0-9a-f]{{64}}$'"
BUILD_STATES = (
    "'queued','building','loading','ready','active','retired','failed','pruning','pruned'"
)
JOB_STATES = "'queued','running','succeeded','failed','cancelled'"
LOCATION_KINDS = "'stop_place','boarding_point','operational_point'"
LOCATION_DOMAINS = "'surface','heavy_rail'"


class SourceRow(Base):
    __tablename__ = "source"
    __table_args__ = {"schema": "control"}  # noqa: RUF012

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    licence: Mapped[str | None] = mapped_column(Text)
    retrieval_method: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SourceConfigRevisionRow(Base):
    __tablename__ = "source_config_revision"
    __table_args__ = (
        UniqueConstraint("source_id", "sha256", name="uq_source_config_digest"),
        CheckConstraint(SHA256_CHECK.format(column="sha256"), name="ck_source_config_sha256"),
        CheckConstraint("schema_version > 0", name="ck_source_config_schema_version"),
        {"schema": "control"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("control.source.id"), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ArtifactRow(Base):
    __tablename__ = "artifact"
    __table_args__ = (
        CheckConstraint(SHA256_CHECK.format(column="sha256"), name="ck_artifact_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_size"),
        CheckConstraint("storage_key !~ '^(?:[A-Za-z]:|/|\\\\)'", name="ck_artifact_relative_key"),
        {"schema": "control"},
    )

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SourceSnapshotRow(Base):
    __tablename__ = "source_snapshot"
    __table_args__ = (
        UniqueConstraint("source_id", "payload_sha256", name="uq_source_snapshot_payload"),
        CheckConstraint(SHA256_CHECK.format(column="payload_sha256"), name="ck_snapshot_payload"),
        CheckConstraint(SHA256_CHECK.format(column="manifest_sha256"), name="ck_snapshot_manifest"),
        {"schema": "control"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("control.source.id"), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    declared_version: Mapped[str | None] = mapped_column(Text)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SourceSnapshotArtifactRow(Base):
    __tablename__ = "source_snapshot_artifact"
    __table_args__ = ({"schema": "control"},)

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("control.source_snapshot.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(96), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(
        ForeignKey("control.artifact.sha256"), nullable=False
    )


class BuildSpecRow(Base):
    __tablename__ = "build_spec"
    __table_args__ = (
        CheckConstraint(SHA256_CHECK.format(column="sha256"), name="ck_build_spec_sha256"),
        CheckConstraint("schema_version > 0", name="ck_build_spec_schema_version"),
        {"schema": "control"},
    )

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BuildJobRow(Base):
    __tablename__ = "build_job"
    __table_args__ = (
        CheckConstraint(f"state IN ({JOB_STATES})", name="ck_build_job_state"),
        CheckConstraint("priority BETWEEN -32768 AND 32767", name="ck_build_job_priority"),
        {"schema": "control"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    build_spec_sha256: Mapped[str] = mapped_column(
        ForeignKey("control.build_spec.sha256"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BuildJobAttemptRow(Base):
    __tablename__ = "build_job_attempt"
    __table_args__ = (
        CheckConstraint("attempt > 0", name="ck_build_attempt_number"),
        {"schema": "control"},
    )

    job_id: Mapped[int] = mapped_column(
        ForeignKey("control.build_job.id", ondelete="CASCADE"), primary_key=True
    )
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    log_artifact_sha256: Mapped[str | None] = mapped_column(ForeignKey("control.artifact.sha256"))


class BuildJobEventRow(Base):
    __tablename__ = "build_job_event"
    __table_args__ = ({"schema": "control"},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("control.build_job.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class StaticBuildRow(Base):
    __tablename__ = "static_build"
    __table_args__ = (
        CheckConstraint(f"state IN ({BUILD_STATES})", name="ck_static_build_state"),
        CheckConstraint(
            "identity_contract IN ('provisional-v0','registry-v1')",
            name="ck_static_build_identity_contract",
        ),
        CheckConstraint(SHA256_CHECK.format(column="build_key_sha256"), name="ck_build_key"),
        CheckConstraint(SHA256_CHECK.format(column="manifest_sha256"), name="ck_build_manifest"),
        CheckConstraint(SHA256_CHECK.format(column="source_set_sha256"), name="ck_build_sources"),
        CheckConstraint(
            SHA256_CHECK.format(column="overlay_policy_sha256"), name="ck_build_policy"
        ),
        CheckConstraint(SHA256_CHECK.format(column="compiler_sha256"), name="ck_build_compiler"),
        CheckConstraint(
            SHA256_CHECK.format(column="compiler_options_sha256"),
            name="ck_build_compiler_options",
        ),
        CheckConstraint(
            "registry_snapshot_sha256 IS NULL OR "
            + SHA256_CHECK.format(column="registry_snapshot_sha256"),
            name="ck_build_registry_snapshot",
        ),
        CheckConstraint(SHA256_CHECK.format(column="gtfs_sha256"), name="ck_build_gtfs"),
        CheckConstraint(SHA256_CHECK.format(column="serving_sha256"), name="ck_build_serving"),
        CheckConstraint(
            SHA256_CHECK.format(column="netex_mapping_sha256"), name="ck_build_netex_mapping"
        ),
        UniqueConstraint("build_key_sha256", name="uq_static_build_key"),
        UniqueConstraint("feed_version", name="uq_static_build_feed_version"),
        {"schema": "control"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feed_version: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    identity_contract: Mapped[str] = mapped_column(String(32), nullable=False)
    build_spec_sha256: Mapped[str] = mapped_column(ForeignKey("control.build_spec.sha256"))
    build_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    overlay_policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    compiler_options_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    gtfs_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    serving_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    netex_mapping_version: Mapped[str] = mapped_column(String(32), nullable=False)
    netex_target_schema: Mapped[str] = mapped_column(String(32), nullable=False)
    netex_extension_version: Mapped[str] = mapped_column(String(32), nullable=False)
    netex_mapping_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    partitions_attached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StaticBuildInputRow(Base):
    __tablename__ = "static_build_input"
    __table_args__ = ({"schema": "control"},)

    build_id: Mapped[int] = mapped_column(
        ForeignKey("control.static_build.id", ondelete="CASCADE"), primary_key=True
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("control.source_snapshot.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(96), primary_key=True)


class BuildArtifactRow(Base):
    __tablename__ = "build_artifact"
    __table_args__ = ({"schema": "control"},)

    build_id: Mapped[int] = mapped_column(
        ForeignKey("control.static_build.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(96), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(
        ForeignKey("control.artifact.sha256"), nullable=False
    )


class BuildValidationRow(Base):
    __tablename__ = "build_validation"
    __table_args__ = ({"schema": "control"},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("control.static_build.id"), nullable=False)
    validator: Mapped[str] = mapped_column(String(96), nullable=False)
    advisory: Mapped[bool] = mapped_column(Boolean, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BuildDiagnosticRow(Base):
    __tablename__ = "build_diagnostic"
    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','error')", name="ck_build_diag_severity"),
        {"schema": "control"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("control.static_build.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(96), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    blocks_activation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class PublicationRow(Base):
    __tablename__ = "publication"
    __table_args__ = ({"schema": "control"},)

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    active_build_id: Mapped[int] = mapped_column(
        ForeignKey("control.static_build.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


PARTITIONED = {"schema": "static", "postgresql_partition_by": "LIST (build_id)"}


class AgencyRow(Base):
    __tablename__ = "agency"
    __table_args__ = (PARTITIONED,)

    build_id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(Text)
    fare_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)


class LocationRow(Base):
    __tablename__ = "location"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "parent_location_id"],
            ["static.location.build_id", "static.location.location_id"],
        ),
        CheckConstraint(f"kind IN ({LOCATION_KINDS})", name="ck_location_kind"),
        CheckConstraint(f"domain IN ({LOCATION_DOMAINS})", name="ck_location_domain"),
        CheckConstraint(
            "(kind = 'boarding_point' AND parent_location_id IS NOT NULL) OR "
            "(kind <> 'boarding_point' AND parent_location_id IS NULL)",
            name="ck_location_parent",
        ),
        CheckConstraint(
            "wheelchair_boarding IS NULL OR wheelchair_boarding BETWEEN 0 AND 2",
            name="ck_location_wheelchair",
        ),
        Index("ix_static_location_position", "position", postgresql_using="gist"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_location_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    public_code: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    municipality_name: Mapped[str | None] = mapped_column(Text)
    district_name: Mapped[str | None] = mapped_column(Text)
    district_code: Mapped[str | None] = mapped_column(String(16))
    nearby_place: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str | None] = mapped_column(String(3))
    coordinate_precision: Mapped[str | None] = mapped_column(String(32))
    position: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False)
    )
    url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(64))
    wheelchair_boarding: Mapped[int | None] = mapped_column(SmallInteger)


class RouteRow(Base):
    __tablename__ = "route"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "agency_id"], ["static.agency.build_id", "static.agency.agency_id"]
        ),
        CheckConstraint("gtfs_route_type >= 0", name="ck_route_type"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agency_id: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
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
    __table_args__ = (
        CheckConstraint("valid_to >= valid_from", name="ck_calendar_validity"),
        CheckConstraint("weekday_mask BETWEEN 0 AND 127", name="ck_calendar_weekdays"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[str] = mapped_column(Text, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    weekday_mask: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class ServiceExceptionRow(Base):
    __tablename__ = "service_exception"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "service_id"],
            ["static.service_calendar.build_id", "static.service_calendar.service_id"],
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[str] = mapped_column(Text, primary_key=True)
    service_date: Mapped[date] = mapped_column(Date, primary_key=True)
    added: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ShapeRow(Base):
    __tablename__ = "shape"
    __table_args__ = (
        Index("ix_static_shape_geometry", "geometry", postgresql_using="gist"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    shape_id: Mapped[str] = mapped_column(Text, primary_key=True)
    generation_method: Mapped[str] = mapped_column(String(32), nullable=False)
    geometry: Mapped[WKBElement | None] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False)
    )


class ShapePointRow(Base):
    __tablename__ = "shape_point"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "shape_id"], ["static.shape.build_id", "static.shape.shape_id"]
        ),
        CheckConstraint("sequence >= 0", name="ck_shape_point_sequence"),
        CheckConstraint(
            "distance_traveled IS NULL OR distance_traveled >= 0",
            name="ck_shape_point_distance",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    shape_id: Mapped[str] = mapped_column(Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    position: Mapped[WKBElement] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    distance_traveled: Mapped[float | None] = mapped_column(Float)


class TripRow(Base):
    __tablename__ = "trip"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "route_id"], ["static.route.build_id", "static.route.route_id"]
        ),
        ForeignKeyConstraint(
            ["build_id", "service_id"],
            ["static.service_calendar.build_id", "static.service_calendar.service_id"],
        ),
        ForeignKeyConstraint(
            ["build_id", "shape_id"], ["static.shape.build_id", "static.shape.shape_id"]
        ),
        CheckConstraint("direction IS NULL OR direction IN (0,1)", name="ck_trip_direction"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    route_id: Mapped[str] = mapped_column(Text, nullable=False)
    service_id: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[int | None] = mapped_column(SmallInteger)
    headsign: Mapped[str | None] = mapped_column(Text)
    short_name: Mapped[str | None] = mapped_column(Text)
    block_key: Mapped[str | None] = mapped_column(Text)
    wheelchair_accessible: Mapped[int | None] = mapped_column(SmallInteger)
    bikes_allowed: Mapped[int | None] = mapped_column(SmallInteger)
    shape_id: Mapped[str | None] = mapped_column(Text)


class TripCallRow(Base):
    __tablename__ = "trip_call"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        ForeignKeyConstraint(
            ["build_id", "location_id"],
            ["static.location.build_id", "static.location.location_id"],
        ),
        ForeignKeyConstraint(
            ["build_id", "boarding_point_id"],
            ["static.location.build_id", "static.location.location_id"],
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
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[str] = mapped_column(Text, nullable=False)
    passenger_service: Mapped[bool] = mapped_column(Boolean, nullable=False)
    boarding_point_id: Mapped[str | None] = mapped_column(Text)
    scheduled_arrival: Mapped[int | None] = mapped_column(Integer)
    scheduled_departure: Mapped[int | None] = mapped_column(Integer)
    scheduled_passage: Mapped[int | None] = mapped_column(Integer)
    pickup_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dropoff_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    timepoint: Mapped[bool] = mapped_column(Boolean, nullable=False)
    stop_headsign: Mapped[str | None] = mapped_column(Text)
    shape_distance_traveled: Mapped[float | None] = mapped_column(Float)


class RouteSegmentRow(Base):
    __tablename__ = "route_segment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        ForeignKeyConstraint(
            ["build_id", "route_id"], ["static.route.build_id", "static.route.route_id"]
        ),
        CheckConstraint("from_sequence > 0", name="ck_route_segment_start"),
        CheckConstraint("to_sequence >= from_sequence", name="ck_route_segment_end"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    from_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    to_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    route_id: Mapped[str] = mapped_column(Text, nullable=False)


class TransferRow(Base):
    __tablename__ = "transfer"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "from_location_id"],
            ["static.location.build_id", "static.location.location_id"],
        ),
        ForeignKeyConstraint(
            ["build_id", "to_location_id"],
            ["static.location.build_id", "static.location.location_id"],
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    transfer_key: Mapped[str] = mapped_column(Text, primary_key=True)
    from_location_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_location_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_route_id: Mapped[str | None] = mapped_column(Text)
    to_route_id: Mapped[str | None] = mapped_column(Text)
    from_trip_id: Mapped[str | None] = mapped_column(Text)
    to_trip_id: Mapped[str | None] = mapped_column(Text)
    transfer_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    minimum_transfer_time: Mapped[int | None] = mapped_column(Integer)


class FareSystemRow(Base):
    __tablename__ = "fare_system"
    __table_args__ = (PARTITIONED,)

    build_id: Mapped[int] = mapped_column(primary_key=True)
    fare_system_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class FareZoneRow(Base):
    __tablename__ = "fare_zone"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "fare_system_id"],
            ["static.fare_system.build_id", "static.fare_system.fare_system_id"],
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    fare_system_id: Mapped[str] = mapped_column(Text, primary_key=True)
    zone_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)


class LocationZoneRow(Base):
    __tablename__ = "location_zone"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "location_id"],
            ["static.location.build_id", "static.location.location_id"],
        ),
        ForeignKeyConstraint(
            ["build_id", "fare_system_id", "zone_id"],
            [
                "static.fare_zone.build_id",
                "static.fare_zone.fare_system_id",
                "static.fare_zone.zone_id",
            ],
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[str] = mapped_column(Text, primary_key=True)
    fare_system_id: Mapped[str] = mapped_column(Text, primary_key=True)
    zone_id: Mapped[str] = mapped_column(Text, primary_key=True)


class CallZoneRow(Base):
    __tablename__ = "call_zone"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id", "sequence"],
            ["static.trip_call.build_id", "static.trip_call.trip_id", "static.trip_call.sequence"],
        ),
        ForeignKeyConstraint(
            ["build_id", "fare_system_id", "zone_id"],
            [
                "static.fare_zone.build_id",
                "static.fare_zone.fare_system_id",
                "static.fare_zone.zone_id",
            ],
        ),
        CheckConstraint("source_order >= 0", name="ck_call_zone_order"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    fare_system_id: Mapped[str] = mapped_column(Text, primary_key=True)
    zone_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ServiceNoteRow(Base):
    __tablename__ = "service_note"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('route_information','service_note','reservation')",
            name="ck_service_note_kind",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="ck_service_note_validity",
        ),
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"), name="ck_service_note_snapshot"
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    service_note_type: Mapped[str | None] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)


class ServiceNoteAssignmentRow(Base):
    __tablename__ = "service_note_assignment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "note_id"], ["static.service_note.build_id", "static.service_note.note_id"]
        ),
        ForeignKeyConstraint(
            ["build_id", "route_id"], ["static.route.build_id", "static.route.route_id"]
        ),
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        CheckConstraint("scope IN ('route','trip')", name="ck_service_note_assignment_scope"),
        CheckConstraint(
            "(scope='route' AND route_id IS NOT NULL AND trip_id IS NULL) OR "
            "(scope='trip' AND trip_id IS NOT NULL AND route_id IS NULL)",
            name="ck_service_note_assignment_target",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    note_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    route_id: Mapped[str | None] = mapped_column(Text)
    trip_id: Mapped[str | None] = mapped_column(Text)


class ServiceFeatureAssignmentRow(Base):
    __tablename__ = "service_feature_assignment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "route_id"], ["static.route.build_id", "static.route.route_id"]
        ),
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        ForeignKeyConstraint(
            ["build_id", "trip_id", "call_sequence"],
            ["static.trip_call.build_id", "static.trip_call.trip_id", "static.trip_call.sequence"],
        ),
        ForeignKeyConstraint(
            ["build_id", "note_id"], ["static.service_note.build_id", "static.service_note.note_id"]
        ),
        CheckConstraint("scope IN ('route','trip','call')", name="ck_service_feature_scope"),
        CheckConstraint(
            "kind IN ('reservation_available','reservation_required',"
            "'wheelchair_accessible_vehicle','partly_wheelchair_accessible_vehicle',"
            "'refreshments_on_vehicle','luggage_transport','bicycle_transport',"
            "'on_request','conditional','self_service_ticketing','integrated_transport',"
            "'not_stopping','diversion','request_stop','exit_only','boarding_only')",
            name="ck_service_feature_kind",
        ),
        CheckConstraint(
            "(scope='route' AND route_id IS NOT NULL AND trip_id IS NULL "
            "AND call_sequence IS NULL) OR "
            "(scope='trip' AND route_id IS NULL AND trip_id IS NOT NULL "
            "AND call_sequence IS NULL) OR "
            "(scope='call' AND route_id IS NULL AND trip_id IS NOT NULL "
            "AND call_sequence IS NOT NULL)",
            name="ck_service_feature_target",
        ),
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"),
            name="ck_service_feature_snapshot",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    route_id: Mapped[str | None] = mapped_column(Text)
    trip_id: Mapped[str | None] = mapped_column(Text)
    call_sequence: Mapped[int | None] = mapped_column(Integer)
    source_code: Mapped[str] = mapped_column(String(1), nullable=False)
    note_id: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)


class LocationFeatureRow(Base):
    __tablename__ = "location_feature"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "location_id"],
            ["static.location.build_id", "static.location.location_id"],
        ),
        CheckConstraint(
            "kind IN ('wheelchair_accessible','refreshments','toilet','accessible_toilet',"
            "'request_stop','urban_transport_interchange','border_control_only',"
            "'visually_impaired_accessible','accessibility_terminal','rail_interchange',"
            "'line_interchange','metro_interchange','ship_terminal','airport_nearby',"
            "'park_and_ride')",
            name="ck_location_feature_kind",
        ),
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"),
            name="ck_location_feature_snapshot",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    location_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_code: Mapped[str] = mapped_column(String(1), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)


class ConnectionClaimRow(Base):
    __tablename__ = "connection_claim"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "origin_trip_id", "origin_sequence"],
            ["static.trip_call.build_id", "static.trip_call.trip_id", "static.trip_call.sequence"],
        ),
        CheckConstraint("direction IN ('waits_for','connects_to')", name="ck_connection_direction"),
        CheckConstraint(
            "resolution_status IN ('unresolved','pattern','resolved')",
            name="ck_connection_resolution",
        ),
        CheckConstraint(
            "target_derivation IN ('none','structured','spec_note')",
            name="ck_connection_target_derivation",
        ),
        CheckConstraint("wait_minutes IS NULL OR wait_minutes >= 0", name="ck_connection_wait"),
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"), name="ck_connection_snapshot"
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_trip_id: Mapped[str] = mapped_column(Text, nullable=False)
    origin_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    target_source_route_id: Mapped[str | None] = mapped_column(Text)
    target_source_trip_id: Mapped[str | None] = mapped_column(Text)
    target_source_stop_id: Mapped[str | None] = mapped_column(Text)
    target_source_post_id: Mapped[str | None] = mapped_column(Text)
    target_source_end_stop_id: Mapped[str | None] = mapped_column(Text)
    target_source_end_post_id: Mapped[str | None] = mapped_column(Text)
    wait_minutes: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    target_public_line: Mapped[str | None] = mapped_column(Text)
    target_destination_text: Mapped[str | None] = mapped_column(Text)
    target_derivation: Mapped[str] = mapped_column(String(16), nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(16), nullable=False)
    target_route_id: Mapped[str | None] = mapped_column(Text)
    target_trip_id: Mapped[str | None] = mapped_column(Text)
    target_location_id: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)


class TravelRestrictionAssignmentRow(Base):
    __tablename__ = "travel_restriction_assignment"
    __table_args__ = (
        CheckConstraint("scope IN ('route_stop','trip_call')", name="ck_travel_restriction_scope"),
        CheckConstraint("group_code IN ('§','A','B','C')", name="ck_travel_restriction_group"),
        CheckConstraint(
            "(scope='route_stop' AND route_id IS NOT NULL AND trip_id IS NULL "
            "AND call_sequence IS NULL) OR "
            "(scope='trip_call' AND route_id IS NULL AND trip_id IS NOT NULL)",
            name="ck_travel_restriction_target",
        ),
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"),
            name="ck_travel_restriction_snapshot",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    route_id: Mapped[str | None] = mapped_column(Text)
    trip_id: Mapped[str | None] = mapped_column(Text)
    source_route_stop_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_sequence: Mapped[int | None] = mapped_column(Integer)
    group_code: Mapped[str] = mapped_column(String(1), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)


class OperationalLocationRow(Base):
    __tablename__ = "operational_location"
    __table_args__ = (
        Index("ix_operational_location_position", "position", postgresql_using="gist"),
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"),
            name="ck_operational_location_snapshot",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_location_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    country_code: Mapped[str] = mapped_column(String(3), nullable=False)
    primary_code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False)
    )
    coordinate_source: Mapped[str | None] = mapped_column(String(64))
    coordinate_source_object_id: Mapped[str | None] = mapped_column(Text)
    coordinate_match_method: Mapped[str | None] = mapped_column(String(64))


class OperationalJourneyRow(Base):
    __tablename__ = "operational_journey"
    __table_args__ = (
        CheckConstraint(f"domain IN ({LOCATION_DOMAINS})", name="ck_operational_journey_domain"),
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"),
            name="ck_operational_journey_snapshot",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_journey_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)


class OperationalCallRow(Base):
    __tablename__ = "operational_call"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "source_id", "source_journey_id"],
            [
                "static.operational_journey.build_id",
                "static.operational_journey.source_id",
                "static.operational_journey.source_journey_id",
            ],
        ),
        ForeignKeyConstraint(
            ["build_id", "source_id", "source_location_id"],
            [
                "static.operational_location.build_id",
                "static.operational_location.source_id",
                "static.operational_location.source_location_id",
            ],
        ),
        CheckConstraint("sequence > 0", name="ck_operational_call_sequence"),
        CheckConstraint(
            "scheduled_arrival IS NOT NULL OR scheduled_departure IS NOT NULL "
            "OR scheduled_passage IS NOT NULL",
            name="ck_operational_call_time",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_journey_id: Mapped[str] = mapped_column(Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_location_id: Mapped[str] = mapped_column(Text, nullable=False)
    passenger_service: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scheduled_arrival: Mapped[int | None] = mapped_column(Integer)
    scheduled_departure: Mapped[int | None] = mapped_column(Integer)
    scheduled_passage: Mapped[int | None] = mapped_column(Integer)
    subsidiary_code: Mapped[str | None] = mapped_column(Text)
    subsidiary_name: Mapped[str | None] = mapped_column(Text)
    active_line_code: Mapped[str | None] = mapped_column(Text)


class SourceEntityMapRow(Base):
    __tablename__ = "source_entity_map"
    __table_args__ = (
        Index("ix_source_entity_public", "build_id", "entity_kind", "public_id"),
        CheckConstraint("identifier_namespace <> ''", name="ck_source_entity_namespace"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identifier_namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_object_id: Mapped[str] = mapped_column(Text, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)


class SourceTripMapRow(Base):
    __tablename__ = "source_trip_map"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        Index(
            "ix_source_trip_resolve",
            "build_id",
            "source_id",
            "trip_namespace",
            "source_trip_id",
            "valid_from",
            "valid_to",
        ),
        CheckConstraint("valid_to >= valid_from", name="ck_source_trip_validity"),
        CheckConstraint("trip_namespace <> ''", name="ck_source_trip_namespace"),
        CheckConstraint(
            "scheduled_start IS NULL OR scheduled_start >= 0", name="ck_source_trip_start"
        ),
        CheckConstraint("scheduled_end IS NULL OR scheduled_end >= 0", name="ck_source_trip_end"),
        CheckConstraint(
            "scheduled_start IS NULL OR scheduled_end IS NULL OR scheduled_end >= scheduled_start",
            name="ck_source_trip_time_order",
        ),
        CheckConstraint(
            "call_pattern_sha256 IS NULL OR " + SHA256_CHECK.format(column="call_pattern_sha256"),
            name="ck_source_trip_call_pattern",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_start: Mapped[int | None] = mapped_column(Integer)
    scheduled_end: Mapped[int | None] = mapped_column(Integer)
    source_route_id: Mapped[str | None] = mapped_column(Text)
    source_direction_id: Mapped[str | None] = mapped_column(Text)
    source_start_location_id: Mapped[str | None] = mapped_column(Text)
    source_end_location_id: Mapped[str | None] = mapped_column(Text)
    source_block_id: Mapped[str | None] = mapped_column(Text)
    source_run_id: Mapped[str | None] = mapped_column(Text)
    source_duty_id: Mapped[str | None] = mapped_column(Text)
    call_pattern_sha256: Mapped[str | None] = mapped_column(String(64))
    variant_key: Mapped[str | None] = mapped_column(Text)


class SourceCallMapRow(Base):
    __tablename__ = "source_call_map"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id", "call_sequence"],
            ["static.trip_call.build_id", "static.trip_call.trip_id", "static.trip_call.sequence"],
        ),
        CheckConstraint("trip_namespace <> ''", name="ck_source_call_trip_namespace"),
        CheckConstraint("call_namespace <> ''", name="ck_source_call_namespace"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    call_namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_sequence: Mapped[str] = mapped_column(Text, primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    call_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)


class SourceTripCoverageRow(Base):
    __tablename__ = "source_trip_coverage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        CheckConstraint("from_sequence > 0", name="ck_source_coverage_start"),
        CheckConstraint("to_sequence >= from_sequence", name="ck_source_coverage_end"),
        CheckConstraint("trip_namespace <> ''", name="ck_source_coverage_trip_namespace"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    coverage_id: Mapped[str] = mapped_column(Text, primary_key=True)
    trip_namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    source_trip_id: Mapped[str] = mapped_column(Text, nullable=False)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    from_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    to_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_type: Mapped[str] = mapped_column(String(32), nullable=False)
    system_id: Mapped[str | None] = mapped_column(Text)
    coverage_role: Mapped[str | None] = mapped_column(String(32))


class IdentifierAliasRow(Base):
    __tablename__ = "identifier_alias"
    __table_args__ = (
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_alias_validity"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_id: Mapped[str] = mapped_column(Text, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date | None] = mapped_column(Date)
    canonical_value: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class RoadRouteKeyRow(Base):
    __tablename__ = "road_route_key"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "route_id"], ["static.route.build_id", "static.route.route_id"]
        ),
        Index("ix_road_route_key", "build_id", "cis_line_id", "valid_from", "valid_to"),
        CheckConstraint("cis_line_id ~ '^[0-9]{6}$'", name="ck_road_route_cis_line"),
        CheckConstraint("valid_to >= valid_from", name="ck_road_route_validity"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    cis_line_id: Mapped[str] = mapped_column(String(6), primary_key=True)
    route_id: Mapped[str] = mapped_column(Text, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)


class RoadTripKeyRow(Base):
    __tablename__ = "road_trip_key"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        Index(
            "ix_road_trip_key", "build_id", "cis_line_id", "cis_trip_id", "valid_from", "valid_to"
        ),
        CheckConstraint("cis_line_id ~ '^[0-9]{6}$'", name="ck_road_trip_cis_line"),
        CheckConstraint("cis_trip_id >= 0", name="ck_road_trip_cis_trip"),
        CheckConstraint("valid_to >= valid_from", name="ck_road_trip_validity"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    cis_line_id: Mapped[str] = mapped_column(String(6), primary_key=True)
    cis_trip_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)


class RailTripKeyRow(Base):
    __tablename__ = "rail_trip_key"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "trip_id"], ["static.trip.build_id", "static.trip.trip_id"]
        ),
        Index("ix_rail_trip_key", "build_id", "train_number", "valid_from", "valid_to"),
        CheckConstraint("train_number > 0", name="ck_rail_trip_number"),
        CheckConstraint("valid_to >= valid_from", name="ck_rail_trip_validity"),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    train_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[str] = mapped_column(Text, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)


class SelectedFieldProvenanceRow(Base):
    __tablename__ = "selected_field_provenance"
    __table_args__ = (
        CheckConstraint(
            SHA256_CHECK.format(column="source_snapshot_sha256"),
            name="ck_selected_provenance_snapshot",
        ),
        PARTITIONED,
    )

    build_id: Mapped[int] = mapped_column(primary_key=True)
    object_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_key: Mapped[str] = mapped_column(Text, primary_key=True)
    field_name: Mapped[str] = mapped_column(String(96), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    selection_rule: Mapped[str] = mapped_column(String(160), nullable=False)
