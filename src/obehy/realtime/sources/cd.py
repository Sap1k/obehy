"""ČD train compositions (docs/sources/cd.md): `razeniws.cdis.cz/apiws.svc/getTrains`, the
service behind the composition (řazení) pages of cd.cz, asked per train lazily (a lookup, never
polled).

- `plan`: a ČD train by its train number, its service date and the station to answer from: the
  asked call or the next passenger call, by its SR70 code written as ČD writes stations (CD-Q1:
  `54` and the five-digit SR70, no check digit). Which trains and days ČD answers for (its own,
  around today) is the manifest's `applies`, checked by the broker before `plan`.
- `decode`: the answer is JSON wrapped in parentheses (CD-Q2). Each coach (`Coachs`) is a unit
  in train order: its type (`Bdmpz 896`), coach number, classes and services (icon codes with
  their Czech texts in `SvcDescEnum`). The locomotive is a placeholder without detail
  (CD-Q3). The answer holds from the asked station onward.
"""

from __future__ import annotations

import json
import re
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

SOURCE = "cd"
COMPOSITION = "composition"
COUNTRY = "54"  # CD-Q1: a ČD station code is the UIC country code and the SR70
CLASSES = {"1": (1,), "2": (2,), "12": (1, 2)}  # `12`: a coach with both
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
    "inv": "wheelchair_accessible",
    "mozkolo": "bicycle_reservation_possible",
    "mozzav": "luggage_storage",
    "leh": "couchette",
    "luz": "sleeper",
    "rest": "restaurant",
    "kal": "refreshments",
    "tichy": "quiet_zone",
    "business": "business_compartment",
    "damy": "ladies_compartment",
}


class PayloadError(ValueError):
    """An answer that does not have the documented shape."""


def plan(subject: Subject, view: TripView) -> UpstreamRequest | NotAvailable:
    number = view.keys.get("czptt:train_number")
    if number is None:
        return NotAvailable("the train has no train number")
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


# CD-Q7: a multiple unit's car has a bare series as its type, maybe with a variant (`661`,
# `814.2`, `064`); a coach's type has letters (`Bmz 245`).
SERIES = re.compile(r"^\d{3}(\.\d+)?$")
# CD-Q7: the cab's side in the pictogram's name, front first: a token of its own (`814_l_…`,
# `914_R_…`, `014_M_…`: none).
SIDES = {"l": "front", "r": "rear", "lr": "both", "m": None}


def _kind(image: str, vehicle_type: object) -> UnitKind:
    """CD-Q7: a locomotive's pictogram is `hv_…` (hnací vozidlo); a multiple unit's car has a
    series as its type."""

    if image.lower().startswith("hv"):  # ČD lowercases the first entry's name only
        return "locomotive"
    if isinstance(vehicle_type, str) and SERIES.match(vehicle_type):
        return "unit"
    return "coach"


def _cab(image: str) -> str | None:
    """CD-Q7: a car's driving cab from its pictogram's name: `front` or `rear` where the name
    says the side (`814_l`, `914_R`), `true` where it says only that there is one (a control
    car's `RV_…`, a Panter's end car `661l`, `650L`: the side is where the unit ends)."""

    tokens = image.rsplit(".", 1)[0].split("_")
    for token in tokens[1:]:
        if token.lower() in SIDES:
            return SIDES[token.lower()]
    if tokens[0] == "RV":
        return "true"
    marked = any(len(t) > 1 and t[:-1].isdigit() and t[-1] in "lL" for t in tokens)
    return "true" if marked else None


def _unit(sequence: int, coach: dict[str, Any], texts: dict[str, str]) -> Unit:
    image = str(coach.get("Img") or "").rsplit("/", 1)[-1]
    raw_type = coach.get("Type")
    details: list[tuple[str, str]] = []
    if isinstance(raw_type, str) and raw_type.startswith("#"):
        details.append(("type", raw_type))  # CD-Q3: not a type
        raw_type = None
    kind = _kind(image, raw_type)
    cab = _cab(image)
    if cab is not None:
        details.append(("cab", cab))
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
            classes.extend(CLASSES[code])
            continue
        feature = SERVICES.get(code, f"raw:{SOURCE}:{code}")
        if feature not in features:
            features.append(feature)
    details.extend(_direct_coach(coach.get("DirectCoach")))
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


def _direct_coach(raw: object) -> list[tuple[str, str]]:
    """A through coach (CD-Q5): where it leaves this train and the train it goes on in."""

    if not isinstance(raw, dict):
        return [("direct_coach", str(raw))] if raw else []
    coach = cast(dict[str, Any], raw)
    found: list[tuple[str, str]] = []
    if coach.get("OdpSt"):
        found.append(("direct_coach_leaves_at", str(coach["OdpSt"])))
    train = coach.get("NaslVl")
    if isinstance(train, dict):
        named = cast(dict[str, Any], train)
        parts = (named.get("TrType"), named.get("TrNum"), named.get("TrName"))
        found.append(("direct_coach_train", " ".join(str(p) for p in parts if p)))
    return found


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
