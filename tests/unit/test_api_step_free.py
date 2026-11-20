"""Whether one boards in a wheelchair (`codes.step_free`). Ported from the web's tests when the
rule moved here."""

from __future__ import annotations

from obehy.api import schema
from obehy.api.codes import step_free


def traits(low_floor: bool | None, wheelchair_accessible: bool | None) -> schema.VehicleTraits:
    return schema.VehicleTraits(low_floor=low_floor, wheelchair_accessible=wheelchair_accessible)


def test_the_timetable_holds_while_no_vehicle_is_known() -> None:
    assert step_free("accessible", None) == "accessible"
    assert step_free("booking_recommended", None) == "accessible"
    assert step_free("booking_required", traits(None, None)) == "booking"
    assert step_free("partly", None) == "partly"
    assert step_free(None, None) is None


def test_a_known_vehicle_confirms_or_overrules_it() -> None:
    assert step_free("accessible", traits(False, False)) is None
    assert step_free(None, traits(True, None)) == "accessible"
    assert step_free("partly", traits(None, True)) == "accessible"
    # The carrier still wants the booking, whatever runs.
    assert step_free("booking_required", traits(True, True)) == "booking"
