"""Readable static codes: typed features, amenities and boarding from the versioned codes file
(`src/obehy/data/api/codes-v1.toml`). Pure; the SQL only fetches the rows."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, cast

from obehy.api import schema

CODES = Path(__file__).resolve().parents[1] / "data" / "api" / "codes-v1.toml"

# The strongest wheelchair and bicycle statement wins when a trip has several.
_WHEELCHAIR = (
    ("wheelchair_booking_required", "booking_required"),
    ("wheelchair_booking_recommended", "booking_recommended"),
    ("wheelchair_accessible_vehicle", "accessible"),
    ("partly_wheelchair_accessible_vehicle", "partly"),
)
_BICYCLES = (
    ("bicycle_transport_prohibited", "prohibited"),
    ("bicycle_reservation_required", "reservation_required"),
    ("bicycle_reservation_available", "reservation_available"),
    ("bicycle_transport", "allowed"),
    ("bicycle_carry_on", "allowed"),
    ("bicycle_storage", "allowed"),
)
_WHOLE_TRIP = {"trip", "route", "service"}


@dataclass(frozen=True, slots=True)
class Kind:
    group: str
    label: schema.Label
    icon: str


@dataclass(frozen=True, slots=True)
class Codes:
    version: str
    kinds: Mapping[str, Kind]
    calendar: Mapping[str, schema.Label]
    boarding: Mapping[int, str]
    systems: Mapping[str, tuple[str, str | None]]

    def kind(self, name: str) -> Kind:
        found = self.kinds.get(name)
        if found is None:
            return Kind("other", schema.Label(cs=name, en=name), "other")
        return found

    def boarding_of(self, code: int) -> schema.Boarding:
        return cast(schema.Boarding, self.boarding.get(code, "regular"))

    def system(self, code: str) -> schema.IntegratedSystem:
        name, abbreviation = self.systems.get(code, (None, None))
        return schema.IntegratedSystem(code=code, name=name, abbreviation=abbreviation)


@cache
def load_codes(path: Path = CODES) -> Codes:
    with path.open("rb") as stream:
        document = tomllib.load(stream)

    def label(value: Mapping[str, Any]) -> schema.Label:
        return schema.Label(cs=str(value["cs"]), en=str(value["en"]))

    kinds = cast(dict[str, dict[str, Any]], document["kinds"])
    calendar = cast(dict[str, dict[str, Any]], document["calendar"])
    systems = cast(dict[str, dict[str, Any]], document["integrated_transport"])
    return Codes(
        version=str(document["codes_version"]),
        kinds={k: Kind(str(v["group"]), label(v), str(v["icon"])) for k, v in kinds.items()},
        calendar={k: label(v) for k, v in calendar.items()},
        boarding={int(k): str(v) for k, v in cast(dict[str, str], document["boarding"]).items()},
        systems={
            k: (str(v["name"]), None if v.get("abbreviation") is None else str(v["abbreviation"]))
            for k, v in systems.items()
        },
    )


def feature(codes: Codes, row: Mapping[str, Any]) -> schema.Feature:
    """One assignment row (`trip_features.sql` or `stop_features.sql`) in readable form: the
    raw symbol becomes a label, a calendar text or an integrated transport system."""

    kind = codes.kind(row["kind"])
    code = row.get("source_code")
    value: schema.Label | None = None
    system: schema.IntegratedSystem | None = None
    if row["kind"] == "calendar_designation" and code is not None:
        value = codes.calendar.get(code, schema.Label(cs=code, en=code))
    elif row["kind"] == "integrated_transport" and code is not None and code.isdigit():
        system = codes.system(code)
    note = None
    if row.get("note_kind") is not None:
        note = schema.Note(kind=row["note_kind"], label=row["note_label"], text=row["note_text"])
    return schema.Feature(
        kind=row["kind"],
        group=kind.group,
        label=kind.label,
        icon=kind.icon,
        scope=row["scope"],
        from_sequence=row.get("call_sequence"),
        to_sequence=row.get("call_sequence_to"),
        value=value,
        system=system,
        note=note,
    )


def amenities(features: Iterable[schema.Feature]) -> schema.Amenities:
    """What holds for the whole trip, from its trip-, route- and service-wide features (a
    feature of some calls only stays in `features` with its calls)."""

    whole = [f for f in features if f.scope in _WHOLE_TRIP]
    kinds = {f.kind for f in whole}
    wheelchair = next((value for kind, value in _WHEELCHAIR if kind in kinds), None)
    bicycles = next((value for kind, value in _BICYCLES if kind in kinds), None)
    reservation = (
        "required"
        if "reservation_required" in kinds
        else "available"
        if "reservation_available" in kinds
        else None
    )
    systems = {f.system.code: f.system for f in whole if f.system is not None}
    return schema.Amenities(
        wheelchair=cast(schema.WheelchairAmenity | None, wheelchair),
        bicycles=cast(schema.BicycleAmenity | None, bicycles),
        reservation=reservation,
        refreshments="refreshments_on_vehicle" in kinds,
        luggage="luggage_transport" in kinds,
        self_service_ticketing="self_service_ticketing" in kinds,
        on_request="on_request" in kinds,
        conditional="conditional" in kinds,
        integrated_transport=[systems[code] for code in sorted(systems)],
        operates=[f.value for f in whole if f.kind == "calendar_designation" and f.value],
    )


def call_flags(features: Iterable[schema.Feature], sequence: int) -> list[str]:
    """The stopping features of one call (request stop, exit or boarding only, …), sorted."""

    flags: set[str] = set()
    for f in features:
        if f.group not in ("stopping", "operation") or f.from_sequence is None:
            continue
        last = f.to_sequence if f.to_sequence is not None else f.from_sequence
        if f.from_sequence <= sequence <= last and f.scope in ("call", "call_range"):
            flags.add("request_stop" if f.kind == "on_request" else f.kind)
    return sorted(flags)


def tristate(value: int | None) -> bool | None:
    """GTFS 0 (unknown) / 1 (yes) / 2 (no)."""

    return None if value in (None, 0) else value == 1
