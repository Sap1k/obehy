from __future__ import annotations

from datetime import date

from geoalchemy2.elements import WKTElement
from sqlalchemy import select
from sqlalchemy.orm import Session

from obehy.domain.identifiers import CanonicalId, CisLineId, CisTripId, EntityKind, TrainNumber
from obehy.domain.locations import OperationalCall, PassengerCall
from obehy.domain.schedule import (
    Direction,
    RailTripCandidate,
    RoadTripCandidate,
    ServiceCalendar,
    TrainSegmentMatch,
    TransitMode,
    TripInstance,
    resolve_road_trip,
    resolve_train_segment,
)
from obehy.identity.services import CanonicalRegistry
from obehy.persistence.models import (
    BoardingPointRow,
    CanonicalEntityRow,
    OperationalPointRow,
    RouteRow,
    ScheduledTripRow,
    ServiceCalendarRow,
    ServiceExceptionRow,
    StopPlaceRow,
    TripCallRow,
)


def _point(longitude: float | None, latitude: float | None) -> WKTElement | None:
    if longitude is None and latitude is None:
        return None
    if longitude is None or latitude is None:
        raise ValueError("Longitude and latitude must either both be present or both be absent")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError("Coordinates are outside WGS84 bounds")
    return WKTElement(f"POINT({longitude} {latitude})", srid=4326)


class LocationService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.registry = CanonicalRegistry(session)

    def create_stop_place(
        self, name: str, *, longitude: float | None = None, latitude: float | None = None
    ) -> tuple[CanonicalId, CanonicalId]:
        stop_id = self.registry.allocate(EntityKind.STOP_PLACE)
        fallback_id = self.registry.allocate(EntityKind.BOARDING_POINT)
        self.session.add(
            StopPlaceRow(id=str(stop_id), name=name, centroid=_point(longitude, latitude))
        )
        self.session.flush()
        self.session.add(
            BoardingPointRow(
                id=str(fallback_id),
                stop_place_id=str(stop_id),
                name=None,
                source_code=None,
                is_unspecified=True,
                position=None,
            )
        )
        self.session.flush()
        return stop_id, fallback_id

    def create_boarding_point(
        self,
        stop_place_id: CanonicalId,
        *,
        name: str | None,
        source_code: str | None,
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
                id=str(point_id),
                stop_place_id=str(stop_place_id),
                name=name,
                source_code=source_code,
                is_unspecified=False,
                position=_point(longitude, latitude),
            )
        )
        self.session.flush()
        return point_id

    def create_operational_point(
        self,
        name: str,
        *,
        code: str | None = None,
        longitude: float | None = None,
        latitude: float | None = None,
    ) -> CanonicalId:
        point_id = self.registry.allocate(EntityKind.OPERATIONAL_POINT)
        self.session.add(
            OperationalPointRow(
                id=str(point_id),
                name=name,
                code=code,
                position=_point(longitude, latitude),
            )
        )
        self.session.flush()
        return point_id

    def add_passenger_call(self, trip_id: CanonicalId, call: PassengerCall) -> None:
        boarding = self.session.get(BoardingPointRow, str(call.boarding_point_id))
        if boarding is None or boarding.stop_place_id != str(call.stop_place_id):
            raise ValueError("Scheduled boarding point is not a child of the passenger stop place")
        self.session.add(
            TripCallRow(
                trip_id=str(trip_id),
                sequence=call.sequence,
                location_id=str(call.stop_place_id),
                passenger_service=True,
                scheduled_boarding_point_id=str(call.boarding_point_id),
                scheduled_arrival=None
                if call.scheduled_arrival is None
                else call.scheduled_arrival.seconds,
                scheduled_departure=(
                    None if call.scheduled_departure is None else call.scheduled_departure.seconds
                ),
                scheduled_passage=None,
                pickup_allowed=call.pickup_allowed,
                dropoff_allowed=call.dropoff_allowed,
            )
        )
        self.session.flush()

    def add_operational_call(self, trip_id: CanonicalId, call: OperationalCall) -> None:
        if self.session.get(OperationalPointRow, str(call.operational_point_id)) is None:
            raise ValueError("Unknown operational point")
        self.session.add(
            TripCallRow(
                trip_id=str(trip_id),
                sequence=call.sequence,
                location_id=str(call.operational_point_id),
                passenger_service=False,
                scheduled_boarding_point_id=None,
                scheduled_arrival=None,
                scheduled_departure=None,
                scheduled_passage=call.scheduled_passage.seconds,
                pickup_allowed=False,
                dropoff_allowed=False,
            )
        )
        self.session.flush()


class ScheduleService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.registry = CanonicalRegistry(session)

    def create_calendar(self, calendar: ServiceCalendar) -> int:
        mask = sum(1 << weekday for weekday in calendar.weekdays)
        row = ServiceCalendarRow(
            valid_from=calendar.valid_from,
            valid_to=calendar.valid_to,
            weekday_mask=mask,
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

    def create_road_route(
        self, cis_line_id: CisLineId, mode: TransitMode, public_name: str | None = None
    ) -> CanonicalId:
        route_id = self.registry.allocate(EntityKind.ROUTE)
        self.session.add(
            RouteRow(
                id=str(route_id),
                mode=mode.value,
                cis_line_id=cis_line_id.value,
                public_name=public_name,
            )
        )
        self.session.flush()
        return route_id

    def create_rail_route(self, public_name: str | None = None) -> CanonicalId:
        route_id = self.registry.allocate(EntityKind.ROUTE)
        self.session.add(
            RouteRow(
                id=str(route_id),
                mode=TransitMode.RAIL.value,
                cis_line_id=None,
                public_name=public_name,
            )
        )
        self.session.flush()
        return route_id

    def create_road_trip(
        self,
        *,
        route_id: CanonicalId,
        mode: TransitMode,
        direction: Direction,
        calendar_id: int,
        timetable_variant: str,
        cis_line_id: CisLineId,
        cis_trip_id: CisTripId,
    ) -> CanonicalId:
        trip_id = self.registry.allocate(EntityKind.SCHEDULED_TRIP)
        self.session.add(
            ScheduledTripRow(
                id=str(trip_id),
                route_id=str(route_id),
                mode=mode.value,
                direction=int(direction),
                calendar_id=calendar_id,
                timetable_variant=timetable_variant,
                cis_line_id=cis_line_id.value,
                cis_trip_id=cis_trip_id.value,
                train_number=None,
            )
        )
        self.session.flush()
        return trip_id

    def create_rail_trip(
        self,
        *,
        route_id: CanonicalId,
        direction: Direction,
        calendar_id: int,
        timetable_variant: str,
        train_number: TrainNumber,
    ) -> CanonicalId:
        trip_id = self.registry.allocate(EntityKind.SCHEDULED_TRIP)
        self.session.add(
            ScheduledTripRow(
                id=str(trip_id),
                route_id=str(route_id),
                mode=TransitMode.RAIL.value,
                direction=int(direction),
                calendar_id=calendar_id,
                timetable_variant=timetable_variant,
                cis_line_id=None,
                cis_trip_id=None,
                train_number=train_number.value,
            )
        )
        self.session.flush()
        return trip_id


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
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve_road(
        self, cis_line_id: CisLineId, cis_trip_id: CisTripId, service_date: date
    ) -> TripInstance:
        rows = self.session.scalars(
            select(ScheduledTripRow).where(
                ScheduledTripRow.cis_line_id == cis_line_id.value,
                ScheduledTripRow.cis_trip_id == cis_trip_id.value,
            )
        ).all()
        candidates = [
            RoadTripCandidate(
                canonical_trip_id=CanonicalId(row.id),
                cis_line_id=cis_line_id,
                cis_trip_id=cis_trip_id,
                calendar=_calendar(self.session, row.calendar_id),
            )
            for row in rows
        ]
        return resolve_road_trip(candidates, cis_line_id, cis_trip_id, service_date)

    def resolve_train_segment(
        self,
        train_number: TrainNumber,
        service_date: date,
        direction: Direction,
        segment_stop_place_ids: tuple[CanonicalId, ...],
    ) -> TrainSegmentMatch:
        rows = self.session.scalars(
            select(ScheduledTripRow).where(ScheduledTripRow.train_number == train_number.value)
        ).all()
        candidates: list[RailTripCandidate] = []
        for row in rows:
            calls = self.session.scalars(
                select(TripCallRow)
                .where(TripCallRow.trip_id == row.id, TripCallRow.passenger_service.is_(True))
                .order_by(TripCallRow.sequence)
            ).all()
            candidates.append(
                RailTripCandidate(
                    canonical_trip_id=CanonicalId(row.id),
                    train_number=train_number,
                    direction=Direction(row.direction),
                    calendar=_calendar(self.session, row.calendar_id),
                    passenger_stop_place_ids=tuple(CanonicalId(call.location_id) for call in calls),
                    passenger_call_sequences=tuple(call.sequence for call in calls),
                )
            )
        return resolve_train_segment(
            candidates, train_number, service_date, direction, segment_stop_place_ids
        )


def entity_kind(session: Session, canonical_id: CanonicalId) -> EntityKind:
    row = session.get(CanonicalEntityRow, str(canonical_id))
    if row is None:
        raise LookupError(str(canonical_id))
    return EntityKind(row.kind)


def find_route_by_cis_line(session: Session, cis_line_id: CisLineId) -> RouteRow | None:
    return session.scalar(select(RouteRow).where(RouteRow.cis_line_id == cis_line_id.value))
