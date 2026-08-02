from __future__ import annotations

from datetime import date
from typing import Any

from geoalchemy2.elements import WKTElement
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from obehy.domain.identifiers import CanonicalId, CisLineId, CisTripId, EntityKind, TrainNumber
from obehy.domain.locations import OperationalCall, PassengerCall
from obehy.domain.schedule import (
    Direction,
    LocationDomain,
    RailTripCandidate,
    RoadTripCandidate,
    ServiceCalendar,
    TrainSegmentMatch,
    TransitMode,
    TripInstance,
    resolve_road_trip,
    resolve_train_segment,
)
from obehy.identity.services import AmbiguousIdentityError, CanonicalRegistry
from obehy.persistence.builds import BuildService
from obehy.persistence.models import (
    BoardingPointRevisionRow,
    BoardingPointRow,
    CanonicalEntityRow,
    CanonicalRailTripKeyRow,
    CanonicalRoadRouteKeyRow,
    CanonicalRoadTripKeyRow,
    OperationalPointRevisionRow,
    OperationalPointRow,
    OperatorRevisionRow,
    OperatorRow,
    RouteRevisionRow,
    RouteRow,
    ScheduledTripRow,
    ServiceCalendarRow,
    ServiceExceptionRow,
    StopPlaceRevisionRow,
    StopPlaceRow,
    TripCallRevisionRow,
    TripInstanceRow,
    TripRevisionRow,
)


def _point(longitude: float | None, latitude: float | None) -> WKTElement | None:
    if longitude is None and latitude is None:
        return None
    if longitude is None or latitude is None:
        raise ValueError("Longitude and latitude must either both be present or both be absent")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError("Coordinates are outside WGS84 bounds")
    return WKTElement(f"POINT({longitude} {latitude})", srid=4326)


def _validity(valid_from: date, valid_to: date | None) -> Range[date]:
    if valid_to is not None and valid_to <= valid_from:
        raise ValueError("Validity end must be later than validity start")
    return Range(valid_from, valid_to, bounds="[)")


def _extensions(value: dict[str, Any] | None) -> dict[str, Any]:
    result = value or {}
    for key in result:
        namespace, separator, version = key.partition(":v")
        if not separator or not namespace or not version.isdigit() or int(version) < 1:
            raise ValueError("Extension keys must be schema-versioned namespaces")
    return result


class LocationService:
    def __init__(self, session: Session, build_id: int) -> None:
        self.session = session
        self.build_id = build_id
        self.registry = CanonicalRegistry(session)
        BuildService(session)._require_state(build_id, "building")  # pyright: ignore[reportPrivateUsage]

    def create_stop_place(
        self,
        name: str,
        *,
        domain: LocationDomain,
        longitude: float | None = None,
        latitude: float | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> tuple[CanonicalId, CanonicalId]:
        stop_id = self.registry.allocate(EntityKind.STOP_PLACE)
        fallback_id = self.registry.allocate(EntityKind.BOARDING_POINT)
        self.session.add(StopPlaceRow(id=str(stop_id), location_domain=domain.value))
        self.session.flush()
        self.session.add(
            BoardingPointRow(id=str(fallback_id), stop_place_id=str(stop_id), is_unspecified=True)
        )
        self.session.flush()
        self.session.add(
            StopPlaceRevisionRow(
                build_id=self.build_id,
                stop_place_id=str(stop_id),
                name=name,
                position=_point(longitude, latitude),
                extensions=_extensions(extensions),
            )
        )
        self.session.add(
            BoardingPointRevisionRow(
                build_id=self.build_id,
                boarding_point_id=str(fallback_id),
                name=None,
                public_code=None,
                position=None,
                extensions={},
            )
        )
        self.session.flush()
        return stop_id, fallback_id

    def revise_stop_place(
        self,
        stop_id: CanonicalId,
        *,
        name: str,
        longitude: float | None = None,
        latitude: float | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            StopPlaceRevisionRow(
                build_id=self.build_id,
                stop_place_id=str(stop_id),
                name=name,
                position=_point(longitude, latitude),
                extensions=_extensions(extensions),
            )
        )
        self.session.flush()

    def create_boarding_point(
        self,
        stop_place_id: CanonicalId,
        *,
        name: str | None,
        public_code: str | None,
        longitude: float | None = None,
        latitude: float | None = None,
    ) -> CanonicalId:
        if stop_place_id.kind is not EntityKind.STOP_PLACE:
            raise ValueError("A boarding point parent must be a stop place")
        if self.session.get(StopPlaceRow, str(stop_place_id)) is None:
            raise ValueError("Unknown stop place")
        point_id = self.registry.allocate(EntityKind.BOARDING_POINT)
        self.session.add(
            BoardingPointRow(
                id=str(point_id), stop_place_id=str(stop_place_id), is_unspecified=False
            )
        )
        self.session.flush()
        self.session.add(
            BoardingPointRevisionRow(
                build_id=self.build_id,
                boarding_point_id=str(point_id),
                name=name,
                public_code=public_code,
                position=_point(longitude, latitude),
                extensions={},
            )
        )
        self.session.flush()
        return point_id

    def create_operational_point(
        self,
        name: str,
        *,
        domain: LocationDomain,
        public_code: str | None = None,
        longitude: float | None = None,
        latitude: float | None = None,
    ) -> CanonicalId:
        point_id = self.registry.allocate(EntityKind.OPERATIONAL_POINT)
        self.session.add(OperationalPointRow(id=str(point_id), location_domain=domain.value))
        self.session.flush()
        self.session.add(
            OperationalPointRevisionRow(
                build_id=self.build_id,
                operational_point_id=str(point_id),
                name=name,
                public_code=public_code,
                position=_point(longitude, latitude),
                extensions={},
            )
        )
        self.session.flush()
        return point_id

    def add_passenger_call(self, trip_id: CanonicalId, call: PassengerCall) -> None:
        boarding = self.session.get(BoardingPointRow, str(call.boarding_point_id))
        if boarding is None or boarding.stop_place_id != str(call.stop_place_id):
            raise ValueError("Scheduled boarding point is not a child of the passenger stop place")
        self.session.add(
            TripCallRevisionRow(
                build_id=self.build_id,
                trip_id=str(trip_id),
                sequence=call.sequence,
                location_id=str(call.stop_place_id),
                passenger_service=True,
                scheduled_boarding_point_id=str(call.boarding_point_id),
                scheduled_arrival=None
                if call.scheduled_arrival is None
                else call.scheduled_arrival.seconds,
                scheduled_departure=None
                if call.scheduled_departure is None
                else call.scheduled_departure.seconds,
                scheduled_passage=None,
                pickup_type=0 if call.pickup_allowed else 1,
                dropoff_type=0 if call.dropoff_allowed else 1,
                timepoint=call.scheduled_arrival is not None
                or call.scheduled_departure is not None,
                extensions={},
            )
        )
        self.session.flush()

    def add_operational_call(self, trip_id: CanonicalId, call: OperationalCall) -> None:
        if self.session.get(OperationalPointRow, str(call.operational_point_id)) is None:
            raise ValueError("Unknown operational point")
        self.session.add(
            TripCallRevisionRow(
                build_id=self.build_id,
                trip_id=str(trip_id),
                sequence=call.sequence,
                location_id=str(call.operational_point_id),
                passenger_service=False,
                scheduled_boarding_point_id=None,
                scheduled_arrival=None,
                scheduled_departure=None,
                scheduled_passage=(
                    None if call.scheduled_passage is None else call.scheduled_passage.seconds
                ),
                pickup_type=1,
                dropoff_type=1,
                timepoint=call.scheduled_passage is not None,
                extensions={},
            )
        )
        self.session.flush()


class ScheduleService:
    def __init__(self, session: Session, build_id: int) -> None:
        self.session = session
        self.build_id = build_id
        self.registry = CanonicalRegistry(session)
        BuildService(session)._require_state(build_id, "building")  # pyright: ignore[reportPrivateUsage]

    def create_operator(
        self,
        name: str,
        *,
        timezone: str = "Europe/Prague",
        url: str | None = None,
    ) -> CanonicalId:
        operator_id = self.registry.allocate(EntityKind.OPERATOR)
        self.session.add(OperatorRow(id=str(operator_id)))
        self.session.flush()
        self.session.add(
            OperatorRevisionRow(
                build_id=self.build_id,
                operator_id=str(operator_id),
                name=name,
                timezone=timezone,
                url=url,
                extensions={},
            )
        )
        self.session.flush()
        return operator_id

    def revise_operator(
        self, operator_id: CanonicalId, *, name: str, timezone: str, url: str | None = None
    ) -> None:
        self.session.add(
            OperatorRevisionRow(
                build_id=self.build_id,
                operator_id=str(operator_id),
                name=name,
                timezone=timezone,
                url=url,
                extensions={},
            )
        )
        self.session.flush()

    def create_calendar(
        self, calendar: ServiceCalendar, *, source_service_id: str | None = None
    ) -> int:
        mask = sum(1 << weekday for weekday in calendar.weekdays)
        row = ServiceCalendarRow(
            build_id=self.build_id,
            valid_from=calendar.valid_from,
            valid_to=calendar.valid_to,
            weekday_mask=mask,
            source_service_id=source_service_id,
        )
        self.session.add(row)
        self.session.flush()
        self.session.add_all(
            [
                ServiceExceptionRow(calendar_id=row.id, service_date=item, added=True)
                for item in sorted(calendar.added_dates)
            ]
            + [
                ServiceExceptionRow(calendar_id=row.id, service_date=item, added=False)
                for item in sorted(calendar.removed_dates)
            ]
        )
        self.session.flush()
        return row.id

    def create_route(
        self,
        *,
        domain: LocationDomain,
        operator_id: CanonicalId,
        mode: TransitMode,
        gtfs_route_type: int,
        short_name: str | None = None,
        long_name: str | None = None,
    ) -> CanonicalId:
        route_id = self.registry.allocate(EntityKind.ROUTE)
        self.session.add(RouteRow(id=str(route_id), location_domain=domain.value))
        self.session.flush()
        self.session.add(
            RouteRevisionRow(
                build_id=self.build_id,
                route_id=str(route_id),
                operator_id=str(operator_id),
                mode=mode.value,
                gtfs_route_type=gtfs_route_type,
                short_name=short_name,
                long_name=long_name,
                extensions={},
            )
        )
        self.session.flush()
        return route_id

    def revise_route(
        self,
        route_id: CanonicalId,
        *,
        operator_id: CanonicalId,
        mode: TransitMode,
        gtfs_route_type: int,
        short_name: str | None = None,
        long_name: str | None = None,
    ) -> None:
        self.session.add(
            RouteRevisionRow(
                build_id=self.build_id,
                route_id=str(route_id),
                operator_id=str(operator_id),
                mode=mode.value,
                gtfs_route_type=gtfs_route_type,
                short_name=short_name,
                long_name=long_name,
                extensions={},
            )
        )
        self.session.flush()

    def create_road_route(
        self,
        cis_line_id: CisLineId,
        mode: TransitMode,
        operator_id: CanonicalId,
        valid_from: date,
        valid_to: date | None,
        public_name: str | None = None,
    ) -> CanonicalId:
        route_id = self.create_route(
            domain=LocationDomain.SURFACE,
            operator_id=operator_id,
            mode=mode,
            gtfs_route_type={
                TransitMode.TRAM: 0,
                TransitMode.METRO: 1,
                TransitMode.RAIL: 2,
                TransitMode.BUS: 3,
                TransitMode.FERRY: 4,
                TransitMode.CABLE_CAR: 6,
                TransitMode.TROLLEYBUS: 11,
            }[mode],
            short_name=public_name,
        )
        self.session.add(
            CanonicalRoadRouteKeyRow(
                cis_line_id=cis_line_id.value,
                route_id=str(route_id),
                validity=_validity(valid_from, valid_to),
                confidence=1.0,
            )
        )
        self.session.flush()
        return route_id

    def create_rail_route(
        self, operator_id: CanonicalId, public_name: str | None = None
    ) -> CanonicalId:
        return self.create_route(
            domain=LocationDomain.HEAVY_RAIL,
            operator_id=operator_id,
            mode=TransitMode.RAIL,
            gtfs_route_type=2,
            short_name=public_name,
        )

    def create_road_trip(
        self,
        *,
        route_id: CanonicalId,
        direction: Direction | None,
        calendar_id: int,
        cis_line_id: CisLineId,
        cis_trip_id: CisTripId,
        valid_from: date,
        valid_to: date | None,
    ) -> CanonicalId:
        trip_id = self.registry.allocate(EntityKind.SCHEDULED_TRIP)
        self.session.add(ScheduledTripRow(id=str(trip_id), location_domain="surface"))
        self.session.flush()
        self.add_trip_revision(
            trip_id, route_id=route_id, calendar_id=calendar_id, direction=direction
        )
        self.session.add(
            CanonicalRoadTripKeyRow(
                cis_line_id=cis_line_id.value,
                cis_trip_id=cis_trip_id.value,
                trip_id=str(trip_id),
                validity=_validity(valid_from, valid_to),
                confidence=1.0,
            )
        )
        self.session.flush()
        return trip_id

    def create_rail_trip(
        self,
        *,
        route_id: CanonicalId,
        direction: Direction | None,
        calendar_id: int,
        train_number: TrainNumber,
        valid_from: date,
        valid_to: date | None,
    ) -> CanonicalId:
        trip_id = self.registry.allocate(EntityKind.SCHEDULED_TRIP)
        self.session.add(ScheduledTripRow(id=str(trip_id), location_domain="heavy_rail"))
        self.session.flush()
        self.add_trip_revision(
            trip_id, route_id=route_id, calendar_id=calendar_id, direction=direction
        )
        self.session.add(
            CanonicalRailTripKeyRow(
                train_number=train_number.value,
                trip_id=str(trip_id),
                validity=_validity(valid_from, valid_to),
                confidence=1.0,
            )
        )
        self.session.flush()
        return trip_id

    def add_trip_revision(
        self,
        trip_id: CanonicalId,
        *,
        route_id: CanonicalId,
        calendar_id: int,
        direction: Direction | None,
        headsign: str | None = None,
    ) -> None:
        self.session.add(
            TripRevisionRow(
                build_id=self.build_id,
                trip_id=str(trip_id),
                route_id=str(route_id),
                calendar_id=calendar_id,
                direction=None if direction is None else int(direction),
                headsign=headsign,
                extensions={},
            )
        )
        self.session.flush()


def _calendar(session: Session, calendar_id: int) -> ServiceCalendar:
    row = session.get(ServiceCalendarRow, calendar_id)
    if row is None:
        raise LookupError(f"Unknown service calendar {calendar_id}")
    exceptions = session.scalars(
        select(ServiceExceptionRow).where(ServiceExceptionRow.calendar_id == calendar_id)
    ).all()
    weekdays = frozenset(day for day in range(7) if row.weekday_mask & (1 << day))
    return ServiceCalendar(
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        weekdays=weekdays,
        added_dates=frozenset(item.service_date for item in exceptions if item.added),
        removed_dates=frozenset(item.service_date for item in exceptions if not item.added),
    )


class TripResolver:
    def __init__(
        self,
        session: Session,
        build_id: int | None = None,
        *,
        publication: str | None = None,
    ) -> None:
        if (build_id is None) == (publication is None):
            raise ValueError("Provide exactly one of build_id or publication")
        self.session = session
        self.build_id = (
            build_id
            if build_id is not None
            else BuildService(session).active_build_id(publication or "public")
        )

    def resolve_road(
        self, cis_line_id: CisLineId, cis_trip_id: CisTripId, service_date: date
    ) -> TripInstance:
        rows = self.session.execute(
            select(CanonicalRoadTripKeyRow, TripRevisionRow)
            .join(TripRevisionRow, TripRevisionRow.trip_id == CanonicalRoadTripKeyRow.trip_id)
            .where(
                CanonicalRoadTripKeyRow.cis_line_id == cis_line_id.value,
                CanonicalRoadTripKeyRow.cis_trip_id == cis_trip_id.value,
                CanonicalRoadTripKeyRow.validity.contains(service_date),
                TripRevisionRow.build_id == self.build_id,
            )
        ).all()
        candidates = [
            RoadTripCandidate(
                canonical_trip_id=CanonicalId(key.trip_id),
                cis_line_id=cis_line_id,
                cis_trip_id=cis_trip_id,
                calendar=_calendar(self.session, revision.calendar_id),
            )
            for key, revision in rows
        ]
        instance = resolve_road_trip(candidates, cis_line_id, cis_trip_id, service_date)
        self._materialize_instance(instance)
        return instance

    def resolve_train_segment(
        self,
        train_number: TrainNumber,
        service_date: date,
        direction: Direction | None,
        segment_stop_place_ids: tuple[CanonicalId, ...],
    ) -> TrainSegmentMatch:
        rows = self.session.execute(
            select(CanonicalRailTripKeyRow, TripRevisionRow)
            .join(TripRevisionRow, TripRevisionRow.trip_id == CanonicalRailTripKeyRow.trip_id)
            .where(
                CanonicalRailTripKeyRow.train_number == train_number.value,
                CanonicalRailTripKeyRow.validity.contains(service_date),
                TripRevisionRow.build_id == self.build_id,
            )
        ).all()
        candidates: list[RailTripCandidate] = []
        for key, revision in rows:
            calls = self.session.scalars(
                select(TripCallRevisionRow)
                .where(
                    TripCallRevisionRow.build_id == self.build_id,
                    TripCallRevisionRow.trip_id == key.trip_id,
                    TripCallRevisionRow.passenger_service.is_(True),
                )
                .order_by(TripCallRevisionRow.sequence)
            ).all()
            candidates.append(
                RailTripCandidate(
                    canonical_trip_id=CanonicalId(key.trip_id),
                    train_number=train_number,
                    direction=None if revision.direction is None else Direction(revision.direction),
                    calendar=_calendar(self.session, revision.calendar_id),
                    passenger_stop_place_ids=tuple(CanonicalId(call.location_id) for call in calls),
                    passenger_call_sequences=tuple(call.sequence for call in calls),
                )
            )
        match = resolve_train_segment(
            candidates, train_number, service_date, direction, segment_stop_place_ids
        )
        self._materialize_instance(match.trip_instance)
        return match

    def _materialize_instance(self, instance: TripInstance) -> None:
        if (
            self.session.get(
                TripInstanceRow,
                {
                    "trip_id": str(instance.canonical_trip_id),
                    "operating_date": instance.operating_date,
                },
            )
            is None
        ):
            self.session.add(
                TripInstanceRow(
                    trip_id=str(instance.canonical_trip_id), operating_date=instance.operating_date
                )
            )
            self.session.flush()


def entity_kind(session: Session, canonical_id: CanonicalId) -> EntityKind:
    row = session.get(CanonicalEntityRow, str(canonical_id))
    if row is None:
        raise LookupError(str(canonical_id))
    return EntityKind(row.kind)


def find_route_by_cis_line(
    session: Session, cis_line_id: CisLineId, service_date: date
) -> RouteRow | None:
    keys = session.scalars(
        select(CanonicalRoadRouteKeyRow).where(
            CanonicalRoadRouteKeyRow.cis_line_id == cis_line_id.value,
            CanonicalRoadRouteKeyRow.validity.contains(service_date),
        )
    ).all()
    if len(keys) > 1:
        raise AmbiguousIdentityError(
            "CIS line resolved to multiple canonical routes",
            tuple(sorted({key.route_id for key in keys})),
        )
    return None if not keys else session.get(RouteRow, keys[0].route_id)
