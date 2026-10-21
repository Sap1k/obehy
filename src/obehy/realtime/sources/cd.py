"""ČD train compositions (docs/sources/cd.md): `razeniws.cdis.cz/apiws.svc/getTrains`, the
service behind the composition (řazení) pages of cd.cz, asked per train lazily (a lookup, never
polled).

- `plan`: a ČD train (its CZPTT TR key names company 1154) by its train number, its service date
  and the station to answer from: the asked call or the next passenger call, by its SR70 code
  written as ČD writes stations (CD-Q1: `54` and the five-digit SR70, no check digit).
- `decode`: the answer is JSON wrapped in parentheses (CD-Q2). Each coach (`Coachs`) is a unit
  in train order: its type (`Bdmpz 896`), coach number, classes and services (icon codes with
  their Czech texts in `SvcDescEnum`). The locomotive is a placeholder without detail
  (CD-Q3). The answer holds from the asked station onward.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

from obehy.realtime.compositions import (
    NotAvailable,
    Subject,
    TripView,
    Unit,
    UnitKind,
    UpstreamRequest,
    start_call,
)
from obehy.realtime.times import local_date

SOURCE = "cd"
COMPOSITION = "composition"
COMPANY = "1154"  # ČD's company code in CZPTT (the TR key's second field)
COUNTRY = "54"  # CD-Q1: a ČD station code is the UIC country code and the SR70
# How far from today ČD is asked about a train (its answers are for the days around today).
DAYS_AROUND = 1
CLASSES = {"1": 1, "2": 2}
# ČD's service icons onto the vehicle attribute vocabulary; any other is `raw:cd:<code>`.
SERVICES = {
    "wifi": "wifi",
    "wifi2": "wifi",  # "WiFi is planned in the coach"
    "zas": "power_sockets",
    "neginv": "wheelchair_lift",
    "povkolo": "bicycle_reservation_required",
    "vp": "open_saloon",
    "D": "family_compartment",
    "kino": "children_cinema",
}


class PayloadError(ValueError):
    """An answer that does not have the documented shape."""


def plan(subject: Subject, view: TripView) -> UpstreamRequest | NotAvailable:
    if view.feed != "czptt":
        return NotAvailable("ČD answers trains only")
    tr = view.keys.get("czptt:tr", "")
    if tr.split(":")[1:2] != [COMPANY]:
        return NotAvailable("not a ČD train")
    number = view.keys.get("czptt:train_number")
    if number is None:
        return NotAvailable("the train has no train number")
    if abs(view.service_date - local_date(view.now)) > timedelta(days=DAYS_AROUND):
        return NotAvailable("ČD answers only for trains around today")
    start = start_call(subject, view)
    if isinstance(start, NotAvailable):
        return start
    sr70 = start.keys.get("sr70")
    if sr70 is None:
        return NotAvailable(f"{start.location_id} has no SR70 code")
    station = COUNTRY + sr70
    train = {
        "TrainNum": number,
        "SR70": station,
        "Date": view.service_date.strftime("%d.%m.%Y"),
        "GetPlan": "false",
    }
    return UpstreamRequest(
        cache_key=f"{number}/{view.service_date.isoformat()}/{station}",
        params=(("params", json.dumps([train], separators=(",", ":"))),),
        start=start,
    )


def _json(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8-sig").strip()
    if text.endswith(";"):
        text = text[:-1]
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]  # CD-Q2
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise PayloadError(f"not JSON: {error}") from error
    inner = cast(dict[str, Any], document).get("d") if isinstance(document, dict) else None
    if not isinstance(inner, dict):
        raise PayloadError("no 'd' object")
    return cast(dict[str, Any], inner)


def _unit(sequence: int, coach: dict[str, Any], texts: dict[str, str]) -> Unit:
    image = str(coach.get("Img") or "").rsplit("/", 1)[-1]
    raw_type = coach.get("Type")
    details: list[tuple[str, str]] = []
    if isinstance(raw_type, str) and raw_type.startswith("#"):
        details.append(("type", raw_type))  # CD-Q3: not a type
        raw_type = None
    kind: UnitKind = "locomotive" if image.startswith("hv") else "coach"  # hnací vozidlo
    classes: list[int] = []
    features: list[str] = []
    codes: list[tuple[str, str]] = []
    for service in cast(list[dict[str, Any]], coach.get("Services") or []):
        code = str(service.get("Img") or "")
        if not code:
            continue
        codes.append((code, texts.get(code, "")))
        start, until = service.get("StartStation"), service.get("DestStation")
        if start or until:
            details.append((f"service:{code}", f"{start or ''}/{until or ''}"))
        if code in CLASSES:
            classes.append(CLASSES[code])
            continue
        feature = SERVICES.get(code, f"raw:{SOURCE}:{code}")
        if feature not in features:
            features.append(feature)
    if coach.get("DirectCoach"):
        details.append(("direct_coach", str(coach["DirectCoach"])))
    if coach.get("NumStar"):
        details.append(("number_star", "true"))  # CD-Q4
    notes = [str(n) for n in cast(list[object], coach.get("Notes") or []) if n]
    if notes:
        details.append(("notes", " | ".join(notes)))
    if coach.get("ImgAlt"):
        details.append(("label", str(coach["ImgAlt"])))
    number = coach.get("Num")
    return Unit(
        sequence=sequence,
        kind=kind,
        vehicle_type=str(raw_type) if raw_type else None,
        coach_number=str(number) if number else None,
        classes=tuple(sorted(set(classes))),
        features=tuple(features),
        texts=tuple(codes),
        destination=str(coach["DestSt"]) if coach.get("DestSt") else None,
        details=tuple(details),
    )


def decode(body: bytes, request: UpstreamRequest) -> tuple[Unit, ...]:
    """The units in train order; none when ČD does not know the train then (not found)."""

    del request  # the answer names its train and station; the plan already checked them
    document = _json(body)
    texts = {
        str(item.get("Img")): str(item.get("Text") or "")
        for item in cast(list[dict[str, Any]], document.get("SvcDescEnum") or [])
    }
    trains = cast(list[dict[str, Any]], document.get("Trains") or [])
    if not trains:
        return ()
    coaches = cast(list[dict[str, Any]], trains[0].get("Coachs") or [])
    return tuple(_unit(sequence, coach, texts) for sequence, coach in enumerate(coaches, 1))
