"""MOTIS itineraries enriched with what Oběhy knows of their trips (`motis.py`).

A client plans with MOTIS (Transitous or motis.obehy.cz) and sends each transit leg's trip ID and
stops; the API answers per leg with the Oběhy trip (its realtime, vehicles and the leg's calls) or
says why there is none, and the client keeps MOTIS's data for that leg. The API never calls MOTIS.
A leg resolves exactly or not at all: by the feed's own trip ID, by a source key of the dataset's
namespace, or, for a trip ID of an older timetable version, by the journey key history keeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

from fastapi import APIRouter, Response

from obehy.api import schema
from obehy.api.alerts import find_alerts, trip_alerts
from obehy.api.codes import features, load_codes
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, trip_vehicle
from obehy.api.db import Snapshot
from obehy.api.limits import Limits
from obehy.api.lookup_rules import can_ask
from obehy.api.motis import Dataset, MotisTrip, datasets, parse_stop_id, parse_trip_id
from obehy.api.params import ParameterError
from obehy.api.trips import JOURNEY_NAMESPACES, trip_call, trip_realtime

router = APIRouter(tags=["journeys"])


@dataclass(frozen=True, slots=True)
class _Found:
    outcome: schema.EnrichOutcome
    trip_id: str | None = None
    detail: str | None = None


def _by_key(snap: Snapshot, namespace: str, key: str, service_date: date) -> _Found:
    """The one trip a source key names on the date, as `GET /trips/by-key` finds it."""

    rows = snap.all(
        "trip_by_key", {"namespace": namespace, "key": key, "service_date": service_date}
    )
    if not rows:
        return _Found("not_found", detail=f"no trip {namespace} {key} runs on {service_date}")
    runs = {r["run_key"] for r in rows}
    if len(rows) > 1 and (len(runs) > 1 or None in runs):
        names = ", ".join(r["trip_id"] for r in rows)
        return _Found(
            "ambiguous", detail=f"{namespace} {key} names several trips on {service_date}: {names}"
        )
    return _Found("resolved", trip_id=rows[0]["trip_id"])


def _resolve(snap: Snapshot, motis: MotisTrip, dataset: Dataset) -> _Found:
    if dataset.resolve == "source_key":
        namespace = cast(str, dataset.trip_namespace)
        return _by_key(snap, namespace, motis.trip_id, motis.service_date)
    params = {
        "trip_id": motis.trip_id,
        "service_date": motis.service_date,
        "release_id": snap.release_id,
    }
    row = snap.one("trip", params)
    if row is not None:
        if not row["runs"]:
            return _Found(
                "not_running", detail=f"{motis.trip_id} does not run on {motis.service_date}"
            )
        return _Found("resolved", trip_id=motis.trip_id)
    # MOTIS may run an older release than ours: its trip ID is then of an older timetable version.
    gone = snap.one("trip_gone", params)
    if gone is None:
        return _Found("not_found", detail=f"no trip {motis.trip_id}")
    return _by_key(snap, gone["key_namespace"], gone["key"], motis.service_date)


def _trip(snap: Snapshot, limits: Limits, trip_id: str, service_date: date) -> schema.EnrichedLeg:
    """A resolved trip as an enriched leg with all its calls (`trips.trip`'s reads)."""

    params = {"trip_id": trip_id, "service_date": service_date, "release_id": snap.release_id}
    row = snap.one("trip", params)
    assert row is not None  # resolved against the same snapshot
    codes = load_codes()
    journey = snap.one("trip_journey", params | {"namespaces": JOURNEY_NAMESPACES})
    realtime = snap.one("trip_realtime", params)
    stated = features(
        codes,
        snap.all(
            "trip_features",
            {"trip_id": trip_id, "route_id": row["route_id"], "service_id": row["service_id"]},
        ),
    )
    seen_since = snap.now - timedelta(seconds=limits.vehicle_max_age_s)
    vehicles = snap.all("trip_vehicles", params | {"trip_ids": [trip_id], "seen_since": seen_since})
    calls = snap.all("trip_calls", params)
    alerts = find_alerts(
        snap,
        limits,
        routes=[row["route_id"]],
        stops=sorted({c["location_id"] for c in calls}),
        trips=[trip_id],
    )
    return schema.EnrichedLeg(
        motis_trip_id="",
        outcome="resolved",
        trip=schema.TripRef(trip_id=trip_id, service_date=service_date),
        journey=None if journey is None else schema.JourneyKey.model_validate(journey),
        line=line(row),
        agency=row["agency"],
        trip_short_name=row["trip_short_name"],
        headsign=row["headsign"],
        realtime=None if realtime is None else trip_realtime(realtime),
        vehicles=[trip_vehicle(v, row["mode"]) for v in vehicles],
        composition=can_ask(snap, "composition", trip_id, service_date),
        calls=[
            trip_call(c, stated, first=n == 0, last=n == len(calls) - 1)
            for n, c in enumerate(calls)
        ],
        alerts=trip_alerts(alerts, route=row["route_id"], trip=trip_id),
    )


def _stop_place(
    snap: Snapshot, stop_id: str | None, service_date: date, known: tuple[Dataset, ...]
) -> str | None:
    """The one stop place a MOTIS stop ID names (a platform names its station), or None."""

    if stop_id is None:
        return None
    stop = parse_stop_id(stop_id, known)
    if stop is None:
        return None
    if stop.dataset.resolve == "trip_id":
        public = [stop.stop_id]
    elif stop.dataset.stop_namespace is None:
        return None
    else:
        keyed = snap.all(
            "motis_stop_keys",
            {
                "namespace": stop.dataset.stop_namespace,
                "identifiers": [stop.stop_id],
                "service_date": service_date,
            },
        )
        public = [r["public_id"] for r in keyed]
    if not public:
        return None
    places = {r["location_id"] for r in snap.all("stops_by_id", {"location_ids": public})}
    return places.pop() if len(places) == 1 else None


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
        if found.trip_id is None:
            legs.append(
                schema.EnrichedLeg(
                    motis_trip_id=asked.trip_id, outcome=found.outcome, detail=found.detail
                )
            )
            continue
        cached = (found.trip_id, motis.service_date)
        if cached not in trips:
            trips[cached] = _trip(snap, limits, found.trip_id, motis.service_date)
        whole = trips[cached]
        span = leg_span(
            whole.calls,
            _stop_place(snap, asked.from_stop_id, motis.service_date, known),
            _stop_place(snap, asked.to_stop_id, motis.service_date, known),
        )
        legs.append(
            whole.model_copy(
                update={
                    "motis_trip_id": asked.trip_id,
                    "calls": whole.calls if span is None else span,
                    "leg_calls": span is not None,
                }
            )
        )
    return schema.Enrichment(**envelope(snap), legs=legs)
