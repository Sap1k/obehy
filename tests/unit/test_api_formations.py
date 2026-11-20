"""How a train's composition is shown (`api/formations.py`): cab ends, stretches, where the
rider is, span ends by public calls. Ported from the web's tests when the rules moved here."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from obehy.api import formations, schema

NOW = datetime.fromisoformat("2026-10-20T09:00:00+02:00")


def call(sequence: int, name: str) -> schema.CompositionCall:
    return schema.CompositionCall(
        sequence=sequence, location_id=f"czptt:location:{name}", name=name
    )


CHEB, MOST, USTI, PRAHA = call(1, "Cheb"), call(3, "Most"), call(5, "Ústí"), call(7, "Praha")


def unit(sequence: int, **over: Any) -> schema.CompositionUnit:
    fields: dict[str, Any] = {
        "source": "cd", "label": None, "sequence": sequence, "kind": "coach", "role": "train",
        "vehicle_id": None, "vehicle_type": "Bmpz 895", "coach_number": str(360 + sequence),
        "classes": [2], "features": [], "destination": None, "details": {}, "from_call": CHEB,
        "to_call": PRAHA, "open": True, "observed_at": NOW,
    }  # fmt: skip
    return schema.CompositionUnit(**(fields | over))


def segment(
    units: list[schema.CompositionUnit],
    start: schema.CompositionCall,
    end: schema.CompositionCall,
    positions: list[int],
) -> schema.CompositionSegment:
    return schema.CompositionSegment(
        from_call=start,
        to_call=end,
        vehicles=formations.vehicles(units, positions, start.sequence, end.sequence),
    )


# 650.002 the whole way; 650.003 attached between Most and Ústí.
UNITS = [unit(1), unit(2, from_call=MOST, to_call=USTI)]
SEGMENTS = [
    segment(UNITS, CHEB, MOST, [0]),
    segment(UNITS, MOST, USTI, [0, 1]),
    segment(UNITS, USTI, PRAHA, [0]),
]


def test_the_segment_a_call_is_in_a_boundary_call_starts_the_next() -> None:
    assert formations.segment_at(SEGMENTS, 2) == 0  # Sokolov
    assert formations.segment_at(SEGMENTS, MOST.sequence) == 1
    assert formations.segment_at(SEGMENTS, 6) == 2
    assert formations.segment_at(SEGMENTS, None) == 0
    assert formations.segment_at(SEGMENTS, PRAHA.sequence) == 2
    assert formations.segment_at([], None) is None


def test_a_vehicle_running_only_part_of_a_segment_is_told() -> None:
    assert [v.partial for v in SEGMENTS[1].vehicles] == [False, False]
    short = [unit(3, to_call=MOST)]
    assert formations.vehicles(short, [0], MOST.sequence - 2, USTI.sequence)[0].partial


def _cabs(units: list[schema.CompositionUnit]) -> list[str]:
    return [
        ("<" if e.front else "") + (">" if e.rear else "") or "-"
        for e in formations.cab_ends(units)
    ]


def test_a_locomotive_is_slanted_at_its_end_a_control_car_always() -> None:
    loco, coach, control = unit(0, kind="locomotive"), unit(1), unit(2, details={"cab": "true"})
    assert _cabs([loco, coach, coach, control]) == ["<", "-", "-", ">"]
    assert _cabs([control, coach, loco]) == ["<", "-", ">"]
    assert _cabs([loco]) == ["<>"]
    # Two push-pull sets coupled cab to cab: each control car away from its locomotive.
    assert _cabs([loco, coach, control, control, coach, loco]) == ["<", "-", ">", "<", "-", ">"]


def test_a_multiple_units_cab_is_where_the_source_names_its_side() -> None:
    def side(cab: str | None = None) -> schema.CompositionUnit:
        return unit(3, kind="unit", details={"cab": cab} if cab else {})

    # Regionova trio and pair (train 1584): 814 front, 014 none, 814 rear | 814 front, 914 rear.
    trains = [side("front"), side(), side("rear"), side("front"), side("rear")]
    assert _cabs(trains) == ["<", "-", ">", "<", ">"]
    assert _cabs([unit(1), side("both")]) == ["-", "<>"]


def test_a_units_cabs_without_a_side_the_first_opens_the_next_closes() -> None:
    def car(cab: bool) -> schema.CompositionUnit:
        return unit(3, kind="unit", details={"cab": "true"} if cab else {})

    # Two InterPanters coupled (`661l` … `660L` twice).
    two = [car(True), car(False), car(True), car(True), car(False), car(True)]
    assert _cabs(two) == ["<", "-", ">", "<", "-", ">"]
    assert _cabs([car(False), car(False)]) == ["-", "-"]


def test_neighbours_running_the_same_vehicles_in_either_order_are_one_stretch() -> None:
    # R 619: one answer from Cheb, the same coaches reversed from Praha-Holešovice.
    a, b = unit(1), unit(2, classes=[1])
    later = NOW.replace(hour=10)
    units = [a, b, *(u.model_copy(update={"observed_at": later}) for u in (b, a))]
    same = [segment(units, CHEB, MOST, [0, 1]), segment(units, MOST, PRAHA, [2, 3])]
    (stretch,) = formations.stretches(units, same)
    assert (stretch.from_call, stretch.to_call, stretch.segments) == (CHEB, PRAHA, [0, 1])
    assert [s.segments for s in formations.stretches(UNITS, SEGMENTS)] == [[0], [1], [2]]
    assert formations.stretch_of(formations.stretches(units, same), 1) == 0


def test_a_span_end_past_the_terminus_reads_as_the_terminus() -> None:
    depot = schema.CompositionCall(sequence=9, location_id="x", name="Praha jih")
    assert formations.public_name(depot, CHEB, PRAHA).name == "Praha"
    assert formations.public_name(MOST, CHEB, PRAHA).name == "Most"
