"""MOTIS itineraries enriched with what Oběhy knows of their trips (`motis.py`).

A client plans with MOTIS (Transitous or motis.obehy.cz) and sends each transit leg's trip ID and
stops; the API answers per leg with the Oběhy trip (its realtime, vehicles and the leg's calls) or
says why there is none, and the client keeps MOTIS's data for that leg. The API never calls MOTIS.
A leg resolves exactly or not at all: by the feed's own trip ID, by a source key of the dataset's
namespace, or, for a trip ID of an older timetable version, by the journey key history keeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import cast

from fastapi import APIRouter, Response

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line
from obehy.api.db import Snapshot
from obehy.api.limits import Limits
from obehy.api.motis import (
    Dataset,
    MotisStop,
    MotisTrip,
    datasets,
    parse_stop_id,
    parse_trip_id,
)
from obehy.api.params import ParameterError
from obehy.api.resolve import TripOnDate, by_key, gone, trip_on
from obehy.api.trips import assemble

router = APIRouter(tags=["journeys"])


@dataclass(frozen=True, slots=True)
class _Unresolved:
    outcome: schema.EnrichOutcome
    detail: str | None


def _resolve(snap: Snapshot, motis: MotisTrip, dataset: Dataset) -> _Unresolved | TripOnDate:
    """The leg's running trip, or why there is none."""

    if dataset.resolve == "source_key":
        found = by_key(snap, cast(str, dataset.trip_namespace), motis.trip_id, motis.service_date)
    else:
        trip = trip_on(snap, motis.trip_id, motis.service_date)
        if trip is not None:
            if trip.runs:
                return trip
            return _Unresolved(
                "not_running", f"{motis.trip_id} does not run on {motis.service_date}"
            )
        # MOTIS may run an older release than ours: its trip ID is then of an older timetable
        # version.
        journey = gone(snap, motis.trip_id, motis.service_date)
        if journey is None:
            return _Unresolved("not_found", f"no trip {motis.trip_id}")
        found = by_key(snap, journey.namespace, journey.key, motis.service_date)
    if found.trip_id is None:
        return _Unresolved(found.outcome, found.detail)
    trip = trip_on(snap, found.trip_id, motis.service_date)
    assert trip is not None  # by_key found it running in the same snapshot
    return trip


def _trip(snap: Snapshot, limits: Limits, trip: TripOnDate) -> schema.EnrichedLeg:
    """A resolved trip as an enriched leg with all its calls (as a trip page shows it)."""

    shown = assemble(snap, limits, trip)
    return schema.EnrichedLeg(
        motis_trip_id="",
        outcome="resolved",
        trip=schema.TripRef(trip_id=trip.trip_id, service_date=trip.service_date),
        journey=shown.journey,
        line=line(shown.row),
        agency=shown.row["agency"],
        trip_short_name=shown.row["trip_short_name"],
        headsign=shown.row["headsign"],
        realtime=shown.realtime,
        vehicles=shown.vehicles,
        composition=shown.composition,
        calls=shown.calls,
        alerts=shown.alerts,
    )


def _stop_places(
    snap: Snapshot, stop_ids: list[str | None], service_date: date, known: tuple[Dataset, ...]
) -> list[str | None]:
    """The one stop place each MOTIS stop ID names (a platform names its station), else None:
    one key lookup per dataset and one place lookup for them all."""

    parsed = [None if i is None else parse_stop_id(i, known) for i in stop_ids]
    public: dict[int, list[str]] = {}
    by_namespace: dict[str, list[int]] = {}
    for n, stop in enumerate(parsed):
        if stop is None:
            continue
        if stop.dataset.resolve == "trip_id":
            public[n] = [stop.stop_id]
        elif stop.dataset.stop_namespace is not None:
            by_namespace.setdefault(stop.dataset.stop_namespace, []).append(n)
    for namespace, positions in by_namespace.items():
        found: dict[str, list[str]] = {}
        for r in snap.all(
            "motis_stop_keys",
            {
                "namespace": namespace,
                "identifiers": sorted({cast(MotisStop, parsed[n]).stop_id for n in positions}),
                "service_date": service_date,
            },
        ):
            found.setdefault(r["identifier"], []).append(r["public_id"])
        for n in positions:
            public[n] = found.get(cast(MotisStop, parsed[n]).stop_id, [])
    asked = sorted({p for ids in public.values() for p in ids})
    place_of: dict[str, str] = {
        r["asked"]: r["location_id"]
        for r in (snap.all("stops_by_id", {"location_ids": asked}) if asked else [])
    }
    places: list[str | None] = []
    for n in range(len(stop_ids)):
        named = {place_of[p] for p in public.get(n, []) if p in place_of}
        places.append(named.pop() if len(named) == 1 else None)
    return places


def stop_sequences(calls: list[schema.TripCall], places: list[str | None]) -> list[int | None]:
    """Which of the trip's calls each stop place is, in order: the first call there after the
    previous one matched, so a stop either side leaves out does not shift the rest."""

    sequences: list[int | None] = []
    start = 0
    for place in places:
        at = (
            next((n for n in range(start, len(calls)) if calls[n].location_id == place), None)
            if place is not None
            else None
        )
        if at is None:
            sequences.append(None)
        else:
            sequences.append(calls[at].sequence)
            start = at + 1
    return sequences


def leg_span(
    calls: list[schema.TripCall], board: str | None, alight: str | None
) -> list[schema.TripCall] | None:
    """The calls from boarding to alighting, by stop place: the first boarding call that an
    alighting call follows (a loop line calls at a place twice). None when either is not found."""

    if board is None or alight is None:
        return None
    for start, call in enumerate(calls):
        if call.location_id != board:
            continue
        for end in range(start + 1, len(calls)):
            if calls[end].location_id == alight:
                return calls[start : end + 1]
    return None


@router.post(
    "/journeys/enrich",
    response_model=schema.Enrichment,
    responses={422: {"model": schema.Problem}},
    summary="What Oběhy knows of the trips of MOTIS itinerary legs, leg by leg",
)
def enrich(
    response: Response, snap: SnapshotDep, limits: LimitsDep, body: schema.EnrichRequest
) -> schema.Enrichment:
    if len(body.legs) > limits.enrich_legs_max:
        raise ParameterError(f"at most {limits.enrich_legs_max} legs at once")
    response.headers["Cache-Control"] = "no-store"
    known = datasets()
    trips: dict[tuple[str, date], schema.EnrichedLeg] = {}
    legs: list[schema.EnrichedLeg] = []
    for asked in body.legs:
        motis = parse_trip_id(asked.trip_id, known)
        if motis is None:
            legs.append(
                schema.EnrichedLeg(
                    motis_trip_id=asked.trip_id,
                    outcome="malformed",
                    detail="not a MOTIS trip ID",
                )
            )
            continue
        if motis.dataset is None:
            legs.append(
                schema.EnrichedLeg(
                    motis_trip_id=asked.trip_id,
                    outcome="unknown_dataset",
                    detail="Oběhy has no data of this dataset",
                )
            )
            continue
        found = _resolve(snap, motis, motis.dataset)
        if isinstance(found, _Unresolved):
            legs.append(
                schema.EnrichedLeg(
                    motis_trip_id=asked.trip_id, outcome=found.outcome, detail=found.detail
                )
            )
            continue
        cached = (found.trip_id, found.service_date)
        if cached not in trips:
            trips[cached] = _trip(snap, limits, found)
        whole = trips[cached]
        board, alight, *stops = _stop_places(
            snap,
            [asked.from_stop_id, asked.to_stop_id, *asked.stop_ids],
            motis.service_date,
            known,
        )
        span = leg_span(whole.calls, board, alight)
        legs.append(
            whole.model_copy(
                update={
                    "motis_trip_id": asked.trip_id,
                    "calls": whole.calls if span is None else span,
                    "leg_calls": span is not None,
                    "stop_sequences": stop_sequences(whole.calls, stops),
                }
            )
        )
    return schema.Enrichment(**envelope(snap), legs=legs)
