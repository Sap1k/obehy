import pytest

from obehy.domain.identifiers import CanonicalId
from obehy.domain.locations import OperationalCall, PassengerCall
from obehy.domain.schedule import ServiceTime


def test_passenger_and_operational_call_kinds_are_strict() -> None:
    PassengerCall(
        sequence=10,
        stop_place_id=CanonicalId("S000000001"),
        boarding_point_id=CanonicalId("P000000001"),
        scheduled_arrival=None,
        scheduled_departure=ServiceTime(25 * 3600),
        pickup_allowed=True,
        dropoff_allowed=False,
    )
    OperationalCall(20, CanonicalId("O000000001"), ServiceTime(90000))
    with pytest.raises(ValueError):
        OperationalCall(20, CanonicalId("S000000001"), ServiceTime(10))
    with pytest.raises(ValueError):
        PassengerCall(
            sequence=30,
            stop_place_id=CanonicalId("O000000001"),
            boarding_point_id=CanonicalId("P000000001"),
            scheduled_arrival=ServiceTime(10),
            scheduled_departure=None,
            pickup_allowed=False,
            dropoff_allowed=False,
        )
