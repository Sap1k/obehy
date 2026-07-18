from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.orm import Session

from obehy.domain.identifiers import CanonicalId, CisLineId, CisTripId, EntityKind, TrainNumber
from obehy.domain.locations import OperationalCall, PassengerCall
from obehy.domain.schedule import Direction, ServiceCalendar, ServiceTime, TransitMode
from obehy.fixtures.projections import load_projection
from obehy.identity.services import AliasService, BindingRequest, SourceIdentityService
from obehy.persistence.services import LocationService, ScheduleService, TripResolver

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"
EFFECTIVE_AT = datetime(2026, 7, 18, tzinfo=UTC)


def test_jdf_churn_resolves_via_documented_mock_authority(db_session: Session) -> None:
    export_a = load_projection(FIXTURES / "jdf/export_a/expected.json")
    export_b = load_projection(FIXTURES / "jdf/export_b/expected.json")
    stop_a = export_a["stops"][0]
    stop_b = export_b["stops"][0]
    location_id, _ = LocationService(db_session).create_stop_place(stop_a["name"])
    identities = SourceIdentityService(db_session)
    authority_id = str(stop_a["authoritative_cis_stop_id"])
    identities.bind(
        BindingRequest(
            "cis-authority-mock",
            EntityKind.STOP_PLACE,
            authority_id,
            location_id,
            EFFECTIVE_AT,
            None,
            "fixture_authority",
            0.99,
        )
    )
    for source_object_id in (stop_a["export_stop_id"], stop_b["export_stop_id"]):
        authoritative_match = identities.resolve(
            "cis-authority-mock",
            EntityKind.STOP_PLACE,
            authority_id,
            EFFECTIVE_AT,
        )
        assert authoritative_match is not None
        identities.bind(
            BindingRequest(
                "national-jdf",
                EntityKind.STOP_PLACE,
                source_object_id,
                authoritative_match,
                EFFECTIVE_AT,
                None,
                "mock_authoritative_cis_stop_id",
                0.99,
            )
        )
    resolved_a = identities.resolve(
        "national-jdf", EntityKind.STOP_PLACE, stop_a["export_stop_id"], EFFECTIVE_AT
    )
    resolved_b = identities.resolve(
        "national-jdf", EntityKind.STOP_PLACE, stop_b["export_stop_id"], EFFECTIVE_AT
    )
    assert stop_a["authoritative_cis_stop_id"] == stop_b["authoritative_cis_stop_id"]
    assert resolved_a == resolved_b == location_id


def test_duk_alias_and_pid_partial_train_exit_criteria(db_session: Session) -> None:
    duk = load_projection(FIXTURES / "duk/expected.json")
    aliases = AliasService(db_session)
    aliases.add(
        source_id="duk",
        identifier_kind="cis_line_id",
        observed_value=duk["observed_cis_line_id"],
        normalized_value=duk["normalized_cis_line_id"],
        valid_from=EFFECTIVE_AT,
        valid_to=None,
        reason="DÚK API-specific encoding",
    )
    assert aliases.normalize_cis_line("duk", "582588", EFFECTIVE_AT) == CisLineId("001588")

    czptt = load_projection(FIXTURES / "czptt/expected.json")
    pid = load_projection(FIXTURES / "pid/expected.json")
    locations = LocationService(db_session)
    stop_ids: dict[str, CanonicalId] = {}
    fallback_ids: dict[str, CanonicalId] = {}
    for key, name in (("lhotka", "Lhotka"), ("smrkov", "Smrkov"), ("jedlova", "Jedlová")):
        stop_ids[key], fallback_ids[key] = locations.create_stop_place(name)
    operational_id = locations.create_operational_point("Borový výhybna", code="54002")

    schedules = ScheduleService(db_session)
    rail_route = schedules.create_rail_route("Syntetický vlak 9001")
    calendar_id = schedules.create_calendar(
        ServiceCalendar(date(2026, 7, 18), date(2026, 7, 18), frozenset(range(7)))
    )
    trip_id = schedules.create_rail_trip(
        route_id=rail_route,
        direction=Direction(czptt["direction"]),
        calendar_id=calendar_id,
        timetable_variant="CZPTT-2026-9001-00",
        train_number=TrainNumber(czptt["train_number"]),
    )
    calls = cast(list[dict[str, object]], czptt["calls"])
    for call in calls:
        sequence = int(cast(int, call["sequence"]))
        location_key = str(call["location_key"])
        if call["kind"] == "passenger":
            locations.add_passenger_call(
                trip_id,
                PassengerCall(
                    sequence=sequence,
                    stop_place_id=stop_ids[location_key],
                    boarding_point_id=fallback_ids[location_key],
                    scheduled_arrival=(
                        None
                        if "arrival" not in call
                        else ServiceTime(int(cast(int, call["arrival"])))
                    ),
                    scheduled_departure=(
                        None
                        if "departure" not in call
                        else ServiceTime(int(cast(int, call["departure"])))
                    ),
                    pickup_allowed=True,
                    dropoff_allowed=True,
                ),
            )
        else:
            locations.add_operational_call(
                trip_id,
                OperationalCall(
                    sequence,
                    operational_id,
                    ServiceTime(int(cast(int, call["passage"]))),
                ),
            )
    segment_keys = cast(list[str], pid["passenger_location_keys"])
    segment = tuple(stop_ids[key] for key in segment_keys)
    match = TripResolver(db_session).resolve_train_segment(
        TrainNumber(pid["train_number"]),
        date.fromisoformat(pid["service_date"]),
        Direction(pid["direction"]),
        segment,
    )
    assert match.trip_instance.canonical_trip_id == trip_id
    assert match.matched_call_sequences == (30, 40)


def test_dated_road_variant_resolution(db_session: Session) -> None:
    schedules = ScheduleService(db_session)
    line = CisLineId("001588")
    route = schedules.create_road_route(line, TransitMode.BUS, "1588")
    calendar_id = schedules.create_calendar(
        ServiceCalendar(date(2026, 7, 18), date(2026, 7, 18), frozenset(range(7)))
    )
    trip_id = schedules.create_road_trip(
        route_id=route,
        mode=TransitMode.BUS,
        direction=Direction.OUTBOUND,
        calendar_id=calendar_id,
        timetable_variant="JDF-distinction-1",
        cis_line_id=line,
        cis_trip_id=CisTripId(7),
    )
    assert (
        TripResolver(db_session)
        .resolve_road(line, CisTripId(7), date(2026, 7, 18))
        .canonical_trip_id
        == trip_id
    )
