from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from obehy.domain.identifiers import CanonicalId, CisLineId, CisTripId, EntityKind, TrainNumber
from obehy.domain.locations import OperationalCall, PassengerCall
from obehy.domain.schedule import (
    Direction,
    LocationDomain,
    ServiceCalendar,
    ServiceTime,
    TransitMode,
)
from obehy.fixtures.projections import load_projection
from obehy.identity.services import AliasService, BindingRequest, SourceIdentityService
from obehy.persistence.builds import BuildService
from obehy.persistence.services import LocationService, ScheduleService, TripResolver

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"
SERVICE_DATE = date(2026, 7, 18)
VALID_TO = date(2026, 7, 19)
HASH = "a" * 64


def create_build(session: Session, version: str) -> int:
    return BuildService(session).create(
        version=version,
        config_sha256=HASH,
        compiler_version="test",
    )


def test_national_domains_are_distinct_and_jdf_churn_is_stable(db_session: Session) -> None:
    export_a = load_projection(FIXTURES / "jdf/export_a/expected.json")
    export_b = load_projection(FIXTURES / "jdf/export_b/expected.json")
    stop_a = export_a["stops"][0]
    stop_b = export_b["stops"][0]
    build_id = create_build(db_session, "domain-separation")
    locations = LocationService(db_session, build_id)
    surface_id, _ = locations.create_stop_place(
        str(stop_a["name"]), domain=LocationDomain.SURFACE, longitude=14.4, latitude=50.1
    )
    rail_id, _ = locations.create_stop_place(
        str(stop_a["name"]), domain=LocationDomain.HEAVY_RAIL, longitude=14.4, latitude=50.1
    )
    assert surface_id != rail_id

    identities = SourceIdentityService(db_session)
    for source_object_id in (str(stop_a["export_stop_id"]), str(stop_b["export_stop_id"])):
        identities.bind(
            BindingRequest(
                "national-jdf",
                EntityKind.STOP_PLACE,
                source_object_id,
                surface_id,
                SERVICE_DATE,
                VALID_TO,
                "fixture_authority",
                0.99,
                location_domain=LocationDomain.SURFACE.value,
            )
        )
    assert identities.resolve(
        "national-jdf", EntityKind.STOP_PLACE, str(stop_a["export_stop_id"]), SERVICE_DATE
    ) == identities.resolve(
        "national-jdf", EntityKind.STOP_PLACE, str(stop_b["export_stop_id"]), SERVICE_DATE
    )
    with pytest.raises(DBAPIError, match="domain"), db_session.begin_nested():
        identities.bind(
            BindingRequest(
                "national-jdf",
                EntityKind.STOP_PLACE,
                "wrong-domain",
                rail_id,
                SERVICE_DATE,
                VALID_TO,
                "manual",
                1.0,
                location_domain=LocationDomain.SURFACE.value,
            )
        )


def test_duk_alias_and_pid_partial_train_exit_criteria(db_session: Session) -> None:
    duk = load_projection(FIXTURES / "duk/expected.json")
    aliases = AliasService(db_session)
    aliases.add(
        source_id="duk",
        identifier_kind="cis_line_id",
        observed_value=str(duk["observed_cis_line_id"]),
        normalized_value=str(duk["normalized_cis_line_id"]),
        valid_from=SERVICE_DATE,
        valid_to=None,
        reason="DÚK API-specific encoding",
    )
    assert aliases.normalize_cis_line("duk", "582588", SERVICE_DATE) == CisLineId("001588")

    build_id = create_build(db_session, "partial-train")
    czptt = load_projection(FIXTURES / "czptt/expected.json")
    pid = load_projection(FIXTURES / "pid/expected.json")
    locations = LocationService(db_session, build_id)
    stop_ids: dict[str, CanonicalId] = {}
    fallback_ids: dict[str, CanonicalId] = {}
    for key, name in (("lhotka", "Lhotka"), ("smrkov", "Smrkov"), ("jedlova", "Jedlová")):
        stop_ids[key], fallback_ids[key] = locations.create_stop_place(
            name, domain=LocationDomain.HEAVY_RAIL
        )
    operational_id = locations.create_operational_point(
        "Borový výhybna", domain=LocationDomain.HEAVY_RAIL, public_code="54002"
    )

    schedules = ScheduleService(db_session, build_id)
    operator_id = schedules.create_operator("Synthetic Rail")
    route_id = schedules.create_rail_route(operator_id, "Syntetický vlak 9001")
    calendar_id = schedules.create_calendar(
        ServiceCalendar(SERVICE_DATE, SERVICE_DATE, frozenset(range(7)))
    )
    trip_id = schedules.create_rail_trip(
        route_id=route_id,
        direction=Direction(cast(int, czptt["direction"])),
        calendar_id=calendar_id,
        train_number=TrainNumber(cast(int, czptt["train_number"])),
        valid_from=SERVICE_DATE,
        valid_to=VALID_TO,
    )
    for call in cast(list[dict[str, object]], czptt["calls"]):
        sequence = int(cast(int, call["sequence"]))
        key = str(call["location_key"])
        if call["kind"] == "passenger":
            locations.add_passenger_call(
                trip_id,
                PassengerCall(
                    sequence=sequence,
                    stop_place_id=stop_ids[key],
                    boarding_point_id=fallback_ids[key],
                    scheduled_arrival=None
                    if "arrival" not in call
                    else ServiceTime(int(cast(int, call["arrival"]))),
                    scheduled_departure=None
                    if "departure" not in call
                    else ServiceTime(int(cast(int, call["departure"]))),
                    pickup_allowed=True,
                    dropoff_allowed=True,
                ),
            )
        else:
            locations.add_operational_call(
                trip_id,
                OperationalCall(
                    sequence, operational_id, ServiceTime(int(cast(int, call["passage"])))
                ),
            )
    segment = tuple(stop_ids[key] for key in cast(list[str], pid["passenger_location_keys"]))
    match = TripResolver(db_session, build_id).resolve_train_segment(
        TrainNumber(cast(int, pid["train_number"])),
        date.fromisoformat(str(pid["service_date"])),
        Direction(cast(int, pid["direction"])),
        segment,
    )
    assert match.trip_instance.canonical_trip_id == trip_id
    assert match.matched_call_sequences == (30, 40)


def test_dated_road_variant_resolution(db_session: Session) -> None:
    build_id = create_build(db_session, "road-resolution")
    schedules = ScheduleService(db_session, build_id)
    operator_id = schedules.create_operator("Synthetic Bus")
    line = CisLineId("001588")
    route_id = schedules.create_road_route(
        line,
        TransitMode.BUS,
        operator_id,
        SERVICE_DATE,
        VALID_TO,
        "1588",
    )
    calendar_id = schedules.create_calendar(
        ServiceCalendar(SERVICE_DATE, SERVICE_DATE, frozenset(range(7)))
    )
    trip_id = schedules.create_road_trip(
        route_id=route_id,
        direction=None,
        calendar_id=calendar_id,
        cis_line_id=line,
        cis_trip_id=CisTripId(7),
        valid_from=SERVICE_DATE,
        valid_to=VALID_TO,
    )
    assert (
        TripResolver(db_session, build_id)
        .resolve_road(line, CisTripId(7), SERVICE_DATE)
        .canonical_trip_id
        == trip_id
    )
