from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from obehy.domain.identifiers import CisLineId, CisTripId, EntityKind
from obehy.domain.locations import PassengerCall
from obehy.domain.schedule import LocationDomain, ServiceCalendar, ServiceTime, TransitMode
from obehy.identity.services import AmbiguousIdentityError, CanonicalRegistry
from obehy.persistence.builds import BuildService
from obehy.persistence.models import (
    OperatorRevisionRow,
    OperatorRow,
    ScheduledTripRow,
    ServiceExceptionRow,
    ShapePointRow,
    ShapeRow,
    SourceObjectRow,
    StaticBuildRow,
    TripCallRevisionRow,
    TripRevisionRow,
)
from obehy.persistence.services import (
    LocationService,
    ScheduleService,
    TripResolver,
    find_route_by_cis_line,
)
from obehy.persistence.sources import SourceSnapshotService

pytestmark = pytest.mark.integration
HASH = "b" * 64


def build(session: Session, version: str) -> int:
    return BuildService(session).create(
        version=version,
        config_sha256=HASH,
        compiler_version="database-v1-test",
    )


def operator_revision(session: Session, build_id: int, suffix: str) -> str:
    operator_id = CanonicalRegistry(session).allocate(EntityKind.OPERATOR)
    session.add(OperatorRow(id=str(operator_id)))
    session.flush()
    session.add(
        OperatorRevisionRow(
            build_id=build_id,
            operator_id=str(operator_id),
            name=f"Operator {suffix}",
            url="not a valid URL but intentionally retained",
            timezone="Europe/Prague",
            extensions={"fixture:v1": {"suffix": suffix}},
        )
    )
    session.flush()
    return str(operator_id)


def test_snapshot_deduplication_artifacts_and_source_objects(db_session: Session) -> None:
    snapshots = SourceSnapshotService(db_session)
    first = snapshots.create_or_get(
        source_id="national-jdf",
        content_sha256="c" * 64,
        retrieved_at=datetime(2026, 8, 2, tzinfo=UTC),
        artifact_key="raw/national-jdf/c/bundle",
        manifest={"schema_version": 1},
    )
    assert first == snapshots.create_or_get(
        source_id="national-jdf",
        content_sha256="c" * 64,
        retrieved_at=datetime(2026, 8, 3, tzinfo=UTC),
        artifact_key="ignored/by/content/deduplication",
    )
    snapshots.add_artifact(
        first,
        logical_role="gtfs",
        storage_key="raw/national-jdf/c/gtfs.zip",
        content_sha256="d" * 64,
        size_bytes=123,
        media_type="application/zip",
    )
    db_session.add(
        SourceObjectRow(
            snapshot_id=first,
            entity_kind="stop_place",
            source_object_id="stop-1",
            location_domain=LocationDomain.SURFACE.value,
            record_locator="stops.txt#stop-1",
        )
    )
    db_session.flush()


def test_hot_identity_indexes_exist(db_session: Session) -> None:
    inspector = inspect(db_session.connection())
    road = {item["name"] for item in inspector.get_indexes("canonical_road_trip_key")}
    rail = {item["name"] for item in inspector.get_indexes("canonical_rail_trip_key")}
    assert "ix_road_trip_cis_pair" in road
    assert "ix_rail_trip_train_number" in rail


def test_mobilitydata_is_advisory_and_ready_payload_is_immutable(db_session: Session) -> None:
    service = BuildService(db_session)
    build_id = build(db_session, "immutable-build")
    operator_id = operator_revision(db_session, build_id, "immutable")
    service.add_validation(
        build_id,
        validator="mobilitydata",
        passed=False,
        report={"errors": 99},
    )
    service.mark_ready(build_id, output_artifact_key="builds/immutable/gtfs.zip")
    row = db_session.get(OperatorRevisionRow, {"build_id": build_id, "operator_id": operator_id})
    assert row is not None
    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        row.name = "forbidden mutation"
        db_session.flush()


def test_ready_payload_cannot_be_moved_to_building_build(db_session: Session) -> None:
    service = BuildService(db_session)
    ready_build_id = build(db_session, "immutable-source-build")
    operator_id = operator_revision(db_session, ready_build_id, "source")
    service.mark_ready(ready_build_id, output_artifact_key="builds/source/gtfs.zip")
    building_build_id = build(db_session, "immutable-target-build")
    row = db_session.get(
        OperatorRevisionRow,
        {"build_id": ready_build_id, "operator_id": operator_id},
    )
    assert row is not None

    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        row.build_id = building_build_id
        db_session.flush()


def test_ready_calendar_exception_cannot_be_moved_to_building_build(
    db_session: Session,
) -> None:
    lifecycle = BuildService(db_session)
    ready_build_id = build(db_session, "immutable-calendar-source")
    service_date = date(2026, 7, 18)
    ready_calendar_id = ScheduleService(db_session, ready_build_id).create_calendar(
        ServiceCalendar(
            service_date,
            service_date,
            frozenset(),
            added_dates=frozenset({service_date}),
        )
    )
    lifecycle.mark_ready(ready_build_id, output_artifact_key="builds/calendar-source/gtfs.zip")
    building_build_id = build(db_session, "immutable-calendar-target")
    building_calendar_id = ScheduleService(db_session, building_build_id).create_calendar(
        ServiceCalendar(service_date, service_date, frozenset())
    )
    row = db_session.get(
        ServiceExceptionRow,
        {"calendar_id": ready_calendar_id, "service_date": service_date},
    )
    assert row is not None

    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        row.calendar_id = building_calendar_id
        db_session.flush()


def test_overlapping_cis_route_keys_are_ambiguous(db_session: Session) -> None:
    build_id = build(db_session, "ambiguous-route-key")
    schedules = ScheduleService(db_session, build_id)
    operator_id = schedules.create_operator("Route operator")
    line_id = CisLineId("001588")
    for name in ("first", "second"):
        schedules.create_road_route(
            line_id,
            TransitMode.BUS,
            operator_id,
            date(2026, 7, 1),
            None,
            name,
        )

    with pytest.raises(AmbiguousIdentityError, match="multiple canonical routes"):
        find_route_by_cis_line(db_session, line_id, date(2026, 7, 18))


def test_activation_and_last_three_payload_retention(db_session: Session) -> None:
    service = BuildService(db_session)
    builds: list[int] = []
    operator_ids: list[str] = []
    for number in range(4):
        build_id = build(db_session, f"retention-{number}")
        operator_ids.append(operator_revision(db_session, build_id, str(number)))
        service.mark_ready(build_id, output_artifact_key=f"builds/{number}/gtfs.zip")
        service.activate(build_id)
        builds.append(build_id)
    assert service.prune_after_activation() == (builds[0],)
    assert service.active_build_id() == builds[-1]
    assert db_session.get(StaticBuildRow, builds[0]).state == "pruned"  # type: ignore[union-attr]
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(OperatorRevisionRow)
            .where(OperatorRevisionRow.build_id == builds[0])
        )
        == 0
    )
    for build_id, operator_id in zip(builds[1:], operator_ids[1:], strict=True):
        assert (
            db_session.get(OperatorRevisionRow, {"build_id": build_id, "operator_id": operator_id})
            is not None
        )


def test_shape_points_round_trip_in_source_order(db_session: Session) -> None:
    build_id = build(db_session, "shape-roundtrip")
    db_session.add(
        ShapeRow(
            build_id=build_id,
            shape_id="pid:shape:1",
            generation_method="source",
            extensions={"pid:v1": {"source_shape_id": "1"}},
        )
    )
    db_session.flush()
    db_session.add_all(
        [
            ShapePointRow(
                build_id=build_id,
                shape_id="pid:shape:1",
                sequence=sequence,
                position=WKTElement(f"POINT({14.0 + sequence} 50)", srid=4326),
                distance_traveled=None if sequence == 0 else 1000.0,
            )
            for sequence in (0, 1)
        ]
    )
    db_session.flush()
    points = db_session.scalars(
        select(ShapePointRow)
        .where(ShapePointRow.build_id == build_id, ShapePointRow.shape_id == "pid:shape:1")
        .order_by(ShapePointRow.sequence)
    ).all()
    assert [point.sequence for point in points] == [0, 1]
    assert points[1].distance_traveled == 1000.0


def test_trip_identity_survives_build_revisions_and_untimed_calls(db_session: Session) -> None:
    lifecycle = BuildService(db_session)
    first_build = build(db_session, "trip-lineage-1")
    first_schedule = ScheduleService(db_session, first_build)
    operator_id = first_schedule.create_operator("Stable operator")
    route_id = first_schedule.create_road_route(
        CisLineId("001588"),
        TransitMode.BUS,
        operator_id,
        date(2026, 7, 1),
        None,
        "1588",
    )
    calendar_id = first_schedule.create_calendar(
        ServiceCalendar(date(2026, 7, 1), date(2026, 7, 31), frozenset(range(7)))
    )
    trip_id = first_schedule.create_road_trip(
        route_id=route_id,
        direction=None,
        calendar_id=calendar_id,
        cis_line_id=CisLineId("001588"),
        cis_trip_id=CisTripId(7),
        valid_from=date(2026, 7, 1),
        valid_to=None,
    )
    location_id, fallback_id = LocationService(db_session, first_build).create_stop_place(
        "Surface stop", domain=LocationDomain.SURFACE
    )
    LocationService(db_session, first_build).add_passenger_call(
        trip_id,
        PassengerCall(10, location_id, fallback_id, ServiceTime(25 * 3600), None, True, True),
    )
    lifecycle.mark_ready(first_build, output_artifact_key="builds/lineage-1/gtfs.zip")
    lifecycle.activate(first_build)

    second_build = build(db_session, "trip-lineage-2")
    second_schedule = ScheduleService(db_session, second_build)
    second_schedule.revise_operator(operator_id, name="Stable operator", timezone="Europe/Prague")
    second_schedule.revise_route(
        route_id,
        operator_id=operator_id,
        mode=TransitMode.BUS,
        gtfs_route_type=3,
        short_name="1588",
    )
    second_calendar = second_schedule.create_calendar(
        ServiceCalendar(date(2026, 7, 1), date(2026, 8, 31), frozenset(range(7)))
    )
    second_schedule.add_trip_revision(
        trip_id,
        route_id=route_id,
        calendar_id=second_calendar,
        direction=None,
        headsign="Changed timetable",
    )
    LocationService(db_session, second_build).revise_stop_place(location_id, name="Surface stop")
    db_session.add(
        TripCallRevisionRow(
            build_id=second_build,
            trip_id=str(trip_id),
            sequence=10,
            location_id=str(location_id),
            passenger_service=True,
            scheduled_boarding_point_id=str(fallback_id),
            scheduled_arrival=None,
            scheduled_departure=None,
            scheduled_passage=None,
            pickup_type=0,
            dropoff_type=0,
            timepoint=False,
            extensions={},
        )
    )
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(ScheduledTripRow)) == 1
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(TripRevisionRow)
            .where(TripRevisionRow.trip_id == str(trip_id))
        )
        == 2
    )
    assert (
        TripResolver(db_session, second_build)
        .resolve_road(CisLineId("001588"), CisTripId(7), date(2026, 8, 1))
        .canonical_trip_id
        == trip_id
    )


def test_unversioned_extension_namespace_is_rejected(db_session: Session) -> None:
    build_id = build(db_session, "bad-extension")
    operator_id = CanonicalRegistry(db_session).allocate(EntityKind.OPERATOR)
    db_session.add(OperatorRow(id=str(operator_id)))
    db_session.flush()
    with (
        pytest.raises(DBAPIError, match="schema-versioned namespaces"),
        db_session.begin_nested(),
    ):
        db_session.add(
            OperatorRevisionRow(
                build_id=build_id,
                operator_id=str(operator_id),
                name="Invalid extension",
                timezone="Europe/Prague",
                extensions={"pid": {}},
            )
        )
        db_session.flush()
