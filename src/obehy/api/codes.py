"""Readable static codes (`src/obehy/data/api/codes-v1.toml`). Pure; the SQL only fetches rows.

A feature is one source code as the timetable states it, read with its official Czech text:
a JDF fixed code (Tabulka pevných kódů, JDF 1.11; the text for a trip or for a stop), a CZPTT
central note or a CZPTT train activity (SŽ KADR číselníky). JrUtil types a code into one or
more kinds; the rows of one code at one place become one feature listing those kinds, which
drive the amenities and call flags and give the feature its group, icon and English label.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, cast

from obehy.api import schema

CODES = Path(__file__).resolve().parents[1] / "data" / "api" / "codes-v1.toml"

# The strongest wheelchair and bicycle statement wins when a trip has several.
_WHEELCHAIR: tuple[tuple[str, schema.WheelchairAmenity], ...] = (
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
# Kinds that are a call's flag; CZPTT train activity 0030 (an on-request call) is a request stop.
_FLAGS = {"request_stop", "exit_only", "boarding_only", "not_stopping", "border_control_only"}
_REQUEST_STOP_CODES = {"0030"}
# Which official text of a JDF code a scope reads: a stop's, or a trip's (and its calls').
_SCOPE_TEXT = {"location": "location", "call": "call", "call_range": "call"}


@dataclass(frozen=True, slots=True)
class Kind:
    group: str
    icon: str
    en: str


@dataclass(frozen=True, slots=True)
class CodeText:
    """A source code's official text, per scope where it has one per scope."""

    name: str | None
    texts: Mapping[str, str]  # "trip", "call", "location", or "" for every scope
    reads: str | None = None  # our wording where the official text says nothing to a passenger

    def text(self, scope: str) -> str | None:
        wanted = _SCOPE_TEXT.get(scope, "trip")
        return (
            self.texts.get(wanted)
            or (self.texts.get("trip") if wanted == "call" else None)
            or self.texts.get("")
        )


@dataclass(frozen=True, slots=True)
class Codes:
    version: str
    kinds: Mapping[str, Kind]
    calendar: Mapping[str, schema.Label]
    boarding: Mapping[int, str]
    systems: Mapping[str, tuple[str, str | None]]
    sources: Mapping[str, schema.Label]
    source_codes: Mapping[str, Mapping[str, CodeText]]  # system -> code -> its texts

    def kind(self, name: str) -> Kind:
        return self.kinds.get(name) or Kind("other", "other", name)

    def source(self, name: str) -> schema.Label | None:
        return self.sources.get(name)

    def source_code(
        self, kinds: Iterable[str], code: str | None
    ) -> tuple[schema.SourceSystem, CodeText] | None:
        """The code a feature comes from and its texts: a calendar designation and a fixed
        code are JDF's, a four-digit activity and a central note number CZPTT's."""

        kinds = set(kinds)
        if code is None or kinds == {"integrated_transport"}:
            return None
        systems: tuple[schema.SourceSystem, ...] = (
            ("jdf",) if "calendar_designation" in kinds else ("czptt_activity", "czptt_note", "jdf")
        )
        for system in systems:
            found = self.source_codes.get(system, {}).get(code)
            if found is not None:
                return system, found
        return None

    def note(self, row: Mapping[str, Any]) -> schema.Note:
        """A note; a CZPTT central note by its code: the name it is printed with and the
        official KJŘ text instead of the raw source line."""

        code = row.get("note_code")
        if row["note_kind"] == "czptt_central_note" and code is not None:
            found = self.source_codes.get("czptt_note", {}).get(str(code))
            if found is not None:
                return schema.Note(
                    kind=row["note_kind"],
                    code=str(code),
                    label=row["note_label"],
                    text=found.reads or found.text("trip"),
                )
        return schema.Note(kind=row["note_kind"], label=row["note_label"], text=row["note_text"])

    def boarding_of(self, code: int) -> schema.Boarding:
        return cast(schema.Boarding, self.boarding.get(code, "regular"))

    def system(self, code: str) -> schema.IntegratedSystem:
        name, abbreviation = self.systems.get(code, (None, None))
        return schema.IntegratedSystem(code=code, name=name, abbreviation=abbreviation)


def _code_text(entry: Any) -> CodeText:
    if isinstance(entry, str):
        return CodeText(None, {"": entry})
    fields = cast(dict[str, Any], entry)
    if "text" in fields:
        return CodeText(
            None if fields.get("name") is None else str(fields["name"]),
            {"": str(fields["text"])},
            None if fields.get("reads") is None else str(fields["reads"]),
        )
    return CodeText(None, {scope: str(text) for scope, text in fields.items()})


@cache
def load_codes(path: Path = CODES) -> Codes:
    with path.open("rb") as stream:
        document = tomllib.load(stream)

    def label(value: Mapping[str, Any]) -> schema.Label:
        return schema.Label(cs=str(value["cs"]), en=str(value["en"]))

    kinds = cast(dict[str, dict[str, Any]], document["kinds"])
    systems = cast(dict[str, dict[str, Any]], document["integrated_transport"])
    return Codes(
        version=str(document["codes_version"]),
        kinds={k: Kind(str(v["group"]), str(v["icon"]), str(v["en"])) for k, v in kinds.items()},
        calendar={k: label(v) for k, v in cast(dict[str, Any], document["calendar"]).items()},
        boarding={int(k): str(v) for k, v in cast(dict[str, str], document["boarding"]).items()},
        systems={
            k: (str(v["name"]), None if v.get("abbreviation") is None else str(v["abbreviation"]))
            for k, v in systems.items()
        },
        sources={k: label(v) for k, v in cast(dict[str, Any], document["sources"]).items()},
        source_codes={
            system: {code: _code_text(entry) for code, entry in entries.items()}
            for system, entries in cast(dict[str, dict[str, Any]], document["source_codes"]).items()
        },
    )


def features(codes: Codes, rows: Iterable[Mapping[str, Any]]) -> list[schema.Feature]:
    """Assignment rows (`trip_features.sql`, `stop_features.sql`) as features: the rows one
    source code gives at one place are one feature, read with the code's official text."""

    grouped: defaultdict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for n, row in enumerate(rows):
        code = row.get("source_code")
        key = (
            row["scope"],
            row.get("call_sequence"),
            row.get("call_sequence_to"),
            code if code is not None else f"#{n}",  # a row without a code stands alone
            row.get("note_code"),
            row.get("note_text"),
        )
        grouped[key].append(row)
    return [_feature(codes, group) for group in grouped.values()]


def _feature(codes: Codes, rows: list[Mapping[str, Any]]) -> schema.Feature:
    first = rows[0]
    kinds = sorted({r["kind"] for r in rows})
    kind = codes.kind(first["kind"] if len(kinds) == 1 else _primary(kinds))
    scope, code = first["scope"], first.get("source_code")
    found = codes.source_code(kinds, code)
    source = (
        None
        if found is None or code is None
        else schema.SourceCode(
            system=found[0], code=code, name=found[1].name, text=found[1].text(scope)
        )
    )
    reads = None if found is None else found[1].reads
    system = (
        codes.system(code)
        if "integrated_transport" in kinds and code is not None and code.isdigit()
        else None
    )
    note = None if first.get("note_kind") is None else codes.note(first)
    english = "; ".join(codes.kind(k).en for k in kinds)
    if "calendar_designation" in kinds and code is not None and code in codes.calendar:
        english = codes.calendar[code].en
    czech = reads or (source.text if source is not None and source.text else None)
    if czech is None and note is not None:
        czech = note.text or note.label
    if czech is None and system is not None:
        czech = system.name
    return schema.Feature(
        kinds=kinds,
        group=kind.group,
        icon=kind.icon,
        label=schema.Label(cs=czech or english, en=english),
        official=reads is None and source is not None and source.text is not None,
        scope=scope,
        from_sequence=first.get("call_sequence"),
        to_sequence=first.get("call_sequence_to"),
        system=system,
        note=note,
        source_code=source,
    )


def _primary(kinds: list[str]) -> str:
    """Of the kinds one code gives, the one that names it best (the most specific)."""

    for preferred in (k for k, _ in (*_WHEELCHAIR, *_BICYCLES)):
        if preferred in kinds:
            return preferred
    return kinds[0]


def amenities(features: Iterable[schema.Feature]) -> schema.Amenities:
    """What holds for the whole trip, from its trip-, route- and service-wide features (a
    feature of some calls only stays in `features` with its calls)."""

    whole = [f for f in features if f.scope in _WHOLE_TRIP]
    kinds = {k for f in whole for k in f.kinds}
    wheelchair = wheelchair_of(kinds, None)
    bicycles = next((value for kind, value in _BICYCLES if kind in kinds), None)
    reservation = (
        "required"
        if "reservation_required" in kinds
        else "available"
        if "reservation_available" in kinds
        else None
    )
    systems = {f.system.code: f.system for f in whole if f.system is not None}
    calendar = [f for f in whole if "calendar_designation" in f.kinds]
    return schema.Amenities(
        wheelchair=wheelchair,
        bicycles=cast(schema.BicycleAmenity | None, bicycles),
        reservation=reservation,
        refreshments="refreshments_on_vehicle" in kinds,
        luggage="luggage_transport" in kinds,
        self_service_ticketing="self_service_ticketing" in kinds,
        on_request="on_request" in kinds,
        conditional="conditional" in kinds,
        integrated_transport=[systems[code] for code in sorted(systems)],
        operates=[f.label for f in sorted(calendar, key=_calendar_order)],
    )


def wheelchair_of(kinds: Iterable[str], accessible: bool | None) -> schema.WheelchairAmenity | None:
    """The strongest wheelchair statement of a trip's whole-trip kinds; without one, the GTFS
    `wheelchair_accessible` of the trip (JrUtil's projection of the same codes)."""

    kinds = set(kinds)
    found: schema.WheelchairAmenity | None = next(
        (value for kind, value in _WHEELCHAIR if kind in kinds), None
    )
    if found is None and accessible:
        found = "accessible"
    return found


def _calendar_order(feature: schema.Feature) -> int:
    """Working days, the days of the week, then Sundays and holidays (the `[calendar]` order),
    whatever order the feed states them in."""

    order = list(load_codes().calendar)
    code = feature.source_code.code if feature.source_code is not None else None
    return order.index(code) if code in order else len(order)


def call_flags(
    features: Iterable[schema.Feature],
    sequence: int,
    boarding: schema.Boarding,
    alighting: schema.Boarding,
    *,
    first: bool,
    last: bool,
) -> list[str]:
    """The stopping features of one call (request stop, on request, exit or boarding only,
    …), sorted: the call's own features and what its boarding codes say. No boarding at the
    terminus and no alighting at the origin are what every trip does: they are no flag."""

    flags: set[str] = set()
    for f in features:
        if f.scope not in ("call", "call_range") or f.from_sequence is None:
            continue
        until = f.to_sequence if f.to_sequence is not None else f.from_sequence
        if not f.from_sequence <= sequence <= until:
            continue
        code = f.source_code.code if f.source_code is not None else None
        for kind in f.kinds:
            if kind == "on_request":
                flags.add("request_stop" if code in _REQUEST_STOP_CODES else "on_request")
            elif kind in ("conditional",):
                flags.add("conditional")
            elif kind in _FLAGS:
                flags.add(kind)
    if "ask_driver" in (boarding, alighting):
        flags.add("request_stop")
    if alighting == "none" and boarding != "none" and not first:
        flags.add("boarding_only")
    if boarding == "none" and alighting != "none" and not last:
        flags.add("exit_only")
    return sorted(flags)


def tristate(value: int | None) -> bool | None:
    """GTFS 0 (unknown) / 1 (yes) / 2 (no)."""

    return None if value in (None, 0) else value == 1
