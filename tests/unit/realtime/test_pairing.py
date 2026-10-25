"""Two sources' reports of one journey, side by side, are one vehicle (emit/pairing.py)."""

from __future__ import annotations

from datetime import date, timedelta

from obehy.realtime.emit.pairing import Fix, Pairings
from obehy.realtime.emit.vehicles import VehicleBook
from obehy.realtime.model import JourneyKey
from obehy.reference.registry import Registry
from obehy.reference.vocabulary import load_vocabulary
from tests.realtime.observations import local

SETTINGS = load_vocabulary().pairing
TRIP = JourneyKey("jdf", "cis:line_trip", "582492:143", date(2026, 10, 20))
OTHER = JourneyKey("jdf", "cis:line_trip", "582492:145", date(2026, 10, 20))
DUK, MPV = ("duk", "807"), ("mpvnet", "4U1 2345")
LON, LAT = 13.86, 50.66
STEP = 0.0005  # about 35 m of longitude


def _tick(
    pairings: Pairings, at: str, gap: float = STEP, journey: JourneyKey = TRIP
) -> set[tuple[str, str]]:
    t = local(at)
    fixes = [
        Fix(DUK, TRIP, LON, LAT, t),
        Fix(MPV, journey, LON + gap, LAT, t - timedelta(seconds=10)),
    ]
    return pairings.observe(fixes, t)


def test_a_duk_id_and_an_mpvnet_plate_on_one_journey_become_one_vehicle() -> None:
    pairings = Pairings(SETTINGS)
    assert _tick(pairings, "2026-10-20 08:00") == set()
    assert _tick(pairings, "2026-10-20 08:01") == set()
    assert pairings.partners(DUK) == []  # not yet sustained
    assert _tick(pairings, "2026-10-20 08:02") == {DUK, MPV}  # confirmed
    assert pairings.partners(DUK) == [MPV] and pairings.partners(MPV) == [DUK]
    ((pair, evidence),) = pairings.changes().items()
    assert pair == (DUK, MPV) and evidence is not None and evidence.confirmed


def test_they_part_when_apart_on_another_journey_or_long_unseen() -> None:
    for parting in ("apart", "another journey", "unseen"):
        pairings = Pairings(SETTINGS)
        for minute in range(3):
            _tick(pairings, f"2026-10-20 08:0{minute}")
        assert pairings.partners(DUK) == [MPV]
        if parting == "apart":
            moved = _tick(pairings, "2026-10-20 08:04", gap=0.03)  # about 2 km
        elif parting == "another journey":
            moved = _tick(pairings, "2026-10-20 08:04", journey=OTHER)
        else:
            moved = pairings.observe([], local("2026-10-20 10:00"))
        assert moved == {DUK, MPV}, parting
        assert pairings.partners(DUK) == [], parting


def test_one_source_is_never_paired_with_itself() -> None:
    pairings = Pairings(SETTINGS)
    other_duk = ("duk", "808")
    for minute in range(4):
        t = local(f"2026-10-20 08:0{minute}")
        pairings.observe([Fix(DUK, TRIP, LON, LAT, t), Fix(other_duk, TRIP, LON, LAT, t)], t)
    assert pairings.partners(DUK) == []


DPMD = ("dpmd", "301")


def _three(pairings: Pairings, minute: int, dpmd: bool = True) -> set[tuple[str, str]]:
    t = local(f"2026-10-20 08:0{minute}")
    fixes = [Fix(DUK, TRIP, LON, LAT, t), Fix(MPV, TRIP, LON + STEP, LAT, t)]
    if dpmd:
        fixes.append(Fix(DPMD, TRIP, LON + 2 * STEP, LAT, t))
    return pairings.observe(fixes, t)


def test_pairs_join_transitively_into_one_vehicle() -> None:
    pairings = Pairings(SETTINGS)
    for minute in range(3):
        _three(pairings, minute)
    assert pairings.cluster(MPV) == sorted([DUK, MPV, DPMD])


def test_a_co_located_vehicle_keeps_its_id_as_reports_join_and_leave() -> None:
    book = VehicleBook(Registry(), load_vocabulary())
    day = date(2026, 10, 20)
    pairings = book.pairings
    for minute in range(3):  # MPVnet and DPMD first: DPMD names the vehicle (both unranked)
        t = local(f"2026-10-20 08:0{minute}")
        for r in pairings.observe(
            [Fix(MPV, TRIP, LON, LAT, t), Fix(DPMD, TRIP, LON + STEP, LAT, t)], t
        ):
            book.identify(r, day)
    assert book.identify(MPV, day) == ("dpmd:301", "co_located")
    for minute in range(3, 6):  # DÚK, ranked first, joins: the vehicle keeps its ID
        for r in _three(pairings, minute):
            for m in pairings.cluster(r):
                book.identify(m, day)
    assert {book.identify(r, day) for r in (DUK, MPV, DPMD)} == {("dpmd:301", "co_located")}
