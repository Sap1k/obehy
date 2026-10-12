"""Keyless inference scenarios (`infer/keyless`, BASE_PLAN.md 19.2 and 19.5; ARRIVA-Q2, Q3, Q6,
Q8). An hourly express line A → Teplice,Celní and back; stops 1 km apart."""

from __future__ import annotations

from datetime import date

from obehy.realtime.core import Context, step
from obehy.realtime.index import Index
from obehy.realtime.manifest import semantics_by_channel
from obehy.realtime.model import (
    Delay,
    Destination,
    Effect,
    Fact,
    FeedState,
    JourneyKey,
    LineRef,
    NextStopName,
    Observation,
    ObservationResult,
    Position,
    RawRef,
    Reason,
    VehicleId,
    VehicleKey,
    WriteEvent,
)
from obehy.realtime.policy import load_policy
from obehy.realtime.record import load_channels
from tests.realtime.builder import ORIGIN, STEP_LON, timetable
from tests.realtime.observations import local
from tests.realtime.traces import load

POLICY = load_policy()
SEMANTICS = semantics_by_channel(load_channels())  # arriva-express/buses: floor minutes
D8, D9 = date(2026, 10, 8), date(2026, 10, 9)
TEPLICE = "Teplice,Celní"
OUT = ["A", "B", "C", "D", "E", TEPLICE]
VEHICLE = VehicleId("arriva-express", "6SA3614")


def express(*extra: tuple[str, list[tuple[str, str]]]) -> Index:
    """Trips A → Teplice at 10:00, 11:00 and 12:00, back at 10:00 and 12:00; stops every
    10 minutes."""

    tt = timetable()
    for n, hour in ((1, 10), (3, 11), (5, 12)):
        tt.trip(
            f"580916:{n}",
            days=[D8],
            calls=[(stop, f"{hour}:{10 * i:02}") for i, stop in enumerate(OUT)],
        )
    for n, hour in ((2, 10), (4, 12)):
        tt.trip(
            f"580916:{n}",
            days=[D8],
            calls=[(stop, f"{hour}:{10 * i:02}") for i, stop in enumerate(reversed(OUT))],
        )
    for key, calls in extra:
        tt.trip(key, days=[D8], calls=calls)
    return tt.index()


def at_stop(i: float) -> tuple[float, float]:
    """The point `i` stops along the outbound line (stop 0 is A)."""

    return ORIGIN[0] + i * STEP_LON, ORIGIN[1]


def seen(
    at: str,
    *,
    following: str | None,
    destination: str = TEPLICE,
    delay_min: int | None = 0,
    where: float | None = None,
) -> Observation:
    facts: list[Fact] = [
        VehicleKey(VEHICLE.source_vehicle_id),
        LineRef("cis:line", "580916"),
        Destination(destination),
    ]
    if following is not None:
        facts.append(NextStopName(following))
    if where is not None:
        lon, lat = at_stop(where)
        facts.append(Position(lat=lat, lon=lon))
    if delay_min is not None:
        facts.append(Delay(delay_min * 60, "departure"))
    when = local(at)
    return Observation(
        "arriva-express", "buses", "jdf", when, when, RawRef("0" * 64, 0), 1, tuple(facts)
    )


def run(index: Index, *observations: Observation) -> tuple[FeedState, list[list[Effect]]]:
    state = FeedState("jdf")
    ctx = Context(index, POLICY, semantics=SEMANTICS)
    return state, [step(state, obs, ctx) for obs in observations]


def result(effects: list[Effect]) -> tuple[JourneyKey | None, Reason | None]:
    (found,) = [e for e in effects if isinstance(e, ObservationResult)]
    return found.journey, found.reason


def journey(n: int, day: date = D8) -> JourneyKey:
    return JourneyKey("jdf", "cis:line_trip", f"580916:{n}", day)


def test_position_at_the_origin_binds_the_trip_about_to_leave() -> None:
    state, steps = run(express(), seen("2026-10-08 10:58", following="A", where=0))
    assert result(steps[0]) == (journey(3), None)  # 10:00 is an hour late there; 12:00 too far
    binding = state.vehicles[VEHICLE].binding
    assert binding is not None and binding.method == "inferred" and not binding.confirmed


def test_position_alone_binds_mid_leg() -> None:
    # Halfway C → D at 11:25: on time for the 11:00 trip, 35 minutes late for the 10:00.
    _, steps = run(express(), seen("2026-10-08 11:25", following="D", where=2.5))
    assert result(steps[0]) == (journey(3), None)


def test_arriva_q6_destination_matches_without_commas_and_spaces() -> None:
    obs = seen("2026-10-08 11:25", following="D", where=2.5, destination="Teplice, Celní")
    _, steps = run(express(), obs)
    assert result(steps[0]) == (journey(3), None)
    # The opposite direction never is a candidate.
    _, steps = run(express(), seen("2026-10-08 11:25", following="D", where=2.5, destination="A"))
    assert result(steps[0]) == (None, Reason.NOT_IN_SERVICE)


def test_without_position_or_departure_the_observation_is_ambiguous() -> None:
    _, steps = run(express(), seen("2026-10-08 11:25", following="D"))
    assert result(steps[0]) == (None, Reason.AMBIGUOUS)


def test_arriva_q2_q3_departures_bind_and_confirm() -> None:
    # Untimed by position: the next-stop changes alone decide. Leaving A at 11:01 one minute
    # late (11:00 departure in [10:59:01, 11:00:00]), then B at 11:11.
    state, steps = run(
        express(),
        seen("2026-10-08 10:59", following="A"),
        seen("2026-10-08 11:01", following="B", delay_min=1),
        seen("2026-10-08 11:11", following="C", delay_min=1),
    )
    assert result(steps[0]) == (None, Reason.AMBIGUOUS)
    assert result(steps[1]) == (journey(3), None)
    binding = state.vehicles[VEHICLE].binding
    assert binding is not None and binding.confirmed  # two departures agree


def test_two_trips_minutes_apart_are_ambiguous() -> None:
    late = ("580916:7", [(stop, f"11:{5 + 10 * i:02}") for i, stop in enumerate(OUT)])
    _, steps = run(
        express(late),
        seen("2026-10-08 10:59", following="A"),
        seen("2026-10-08 11:03", following="B", delay_min=2),
    )
    assert result(steps[1]) == (None, Reason.AMBIGUOUS)  # 11:00 and 11:05 both fit


def test_arriva_q8_names_the_trip_lacks_are_no_evidence() -> None:
    # A motorway junction between B and C: leaving it is not a stop of the trip.
    state, steps = run(
        express(),
        seen("2026-10-08 10:59", following="A", where=0),
        seen("2026-10-08 11:01", following="B", delay_min=1, where=0.2),
        seen("2026-10-08 11:11", following="křížení D8", delay_min=1, where=1.2),
        seen("2026-10-08 11:15", following="C", delay_min=1, where=1.6),
    )
    assert [result(s) for s in steps] == [(journey(3), None)] * 4
    episode = state.vehicles[VEHICLE].episode
    assert episode is not None
    assert [d.stop for d in episode.departures] == ["A", "B", "křížení D8"]


def test_history_waits_for_confirmation() -> None:
    _, steps = run(
        express(),
        seen("2026-10-08 10:58", following="A", where=0),
        seen("2026-10-08 11:01", following="B", delay_min=0, where=0.2),
        seen("2026-10-08 11:08", following="B", delay_min=0, where=0.95),
        seen("2026-10-08 11:11", following="C", delay_min=0, where=1.2),
    )
    events = [
        [(e.location_id, e.kind) for e in effects if isinstance(e, WriteEvent)] for effects in steps
    ]
    # Leaving A is seen while only one departure agrees: realtime only. Leaving B is the
    # second; from then on events are history.
    assert events[1] == [] and events[2] == []
    assert ("jdf:B", "departure") in events[3]


def test_a_new_destination_infers_again() -> None:
    state, steps = run(
        express(),
        seen("2026-10-08 11:25", following="D", where=2.5),
        seen("2026-10-08 11:58", following=TEPLICE, destination="A", where=5),
    )
    assert result(steps[0]) == (journey(3), None)
    assert result(steps[1]) == (journey(4), None)  # back from Teplice at 12:00
    binding = state.vehicles[VEHICLE].binding
    assert binding is not None and binding.journey == journey(4)


def test_a_bound_trip_continues_past_midnight_on_its_service_date() -> None:
    times = [f"{23 + (50 + 10 * i) // 60}:{(50 + 10 * i) % 60:02}" for i in range(len(OUT))]
    night = ("580916:9", list(zip(OUT, times, strict=True)))  # 23:50 to 24:40
    _, steps = run(
        express(night),
        seen("2026-10-08 23:55", following="B", where=0.5),
        seen("2026-10-09 00:01", following="C", where=1.1),
        seen("2026-10-09 00:11", following="D", where=2.1),
        seen("2026-10-09 00:21", following="E", where=3.1),
    )
    assert [result(s) for s in steps] == [(journey(9), None)] * 4


def test_a_real_arriva_express_trip_binds_from_its_first_report() -> None:
    # 6SA3453 to Praha on 2026-10-06 (corpus 2026-10-05/06): seven trips of the line run within
    # an hour; the 11:00 one (580916:9) is the one it drives.
    index, observations = load("arriva-580916-9")
    state, steps = run(index, *observations)
    bound = {result(s) for s in steps}
    assert bound == {(journey(9, date(2026, 10, 6)), None)}
    binding = state.vehicles[VehicleId("arriva-express", "6SA3453")].binding
    assert binding is not None and binding.confirmed
    events = [e for effects in steps for e in effects if isinstance(e, WriteEvent)]
    assert events  # history once confirmed
