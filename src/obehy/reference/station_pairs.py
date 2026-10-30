"""The curated station pairs (`jrunify-ext-geodata/stops/station-pairs.csv`): corrections to
the station pairing (release/pairing.py), reviewed in git, mirrored into `ref.station_pair`.

    station_id,stop_id,decision,note

- `station_id` a railway stop place (`czptt:stop:CZ:<SR70>`), `stop_id` a surface one
  (`jdf:stop:<N>`): the public IDs, stable across releases.
- `decision` `pair` makes `stop_id` the station's stop whatever the rule says; `apart` keeps
  `stop_id` from the station, or with no `stop_id` the station from any stop.
- `note` says why, for the reviewer.

A station has at most one `pair`, and a pair is not also kept apart."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import psycopg

from obehy.release.pairing import OVERRIDES

NAME = OVERRIDES
TITLE = "Oběhy station pairs"
COLUMNS = ("station_id", "stop_id", "decision", "note")


@dataclass(frozen=True, slots=True)
class StationPair:
    station_id: str
    stop_id: str
    decision: str
    note: str | None


@dataclass(frozen=True, slots=True)
class StationPairs:
    version: str
    pairs: tuple[StationPair, ...]


def parse(text: str, version: str) -> StationPairs:
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in COLUMNS[:3] if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"{NAME}: no {', '.join(missing)} column")
    pairs: list[StationPair] = []
    seen: dict[tuple[str, str], str] = {}
    forced: dict[str, str] = {}
    for line, row in enumerate(reader, start=2):
        station, stop = (row.get("station_id") or "").strip(), (row.get("stop_id") or "").strip()
        decision = (row.get("decision") or "").strip()
        note = (row.get("note") or "").strip() or None
        if not station.startswith("czptt:"):
            raise ValueError(f"{NAME} line {line}: station_id {station!r} is not a railway stop")
        if stop and not stop.startswith("jdf:"):
            raise ValueError(f"{NAME} line {line}: stop_id {stop!r} is not a surface stop")
        if decision not in ("pair", "apart"):
            raise ValueError(f"{NAME} line {line}: decision {decision!r} is not pair or apart")
        if decision == "pair" and not stop:
            raise ValueError(f"{NAME} line {line}: pair needs a stop_id")
        if (station, stop) in seen:
            raise ValueError(f"{NAME} line {line}: {station} {stop or '(any)'} given twice")
        if decision == "pair":
            if station in forced:
                raise ValueError(
                    f"{NAME} line {line}: {station} already paired with {forced[station]}"
                )
            forced[station] = stop
        seen[(station, stop)] = decision
        pairs.append(StationPair(station, stop, decision, note))
    for (station, stop), decision in seen.items():
        if decision == "apart" and forced.get(station) == stop:
            raise ValueError(f"{NAME}: {station} {stop} is both paired and kept apart")
    return StationPairs(version, tuple(pairs))


def read(path: Path) -> StationPairs:
    data = path.read_bytes()
    return parse(data.decode("utf-8-sig"), hashlib.sha256(data).hexdigest())


def load(connection: psycopg.Connection, pairs: StationPairs) -> bool:
    """Mirror `pairs` into `ref.station_pair` unless this version is loaded; True when it was."""

    with connection.transaction():
        row = connection.execute(
            "SELECT version FROM ref.dataset WHERE dataset = %s FOR UPDATE", (NAME,)
        ).fetchone()
        if row is not None and row[0] == pairs.version:
            return False
        connection.execute("DELETE FROM ref.dataset WHERE dataset = %s", (NAME,))
        connection.execute(
            "INSERT INTO ref.dataset (dataset, version, title, private, priority, rows)"
            " VALUES (%s, %s, %s, false, 0, %s)",
            (NAME, pairs.version, TITLE, len(pairs.pairs)),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO ref.station_pair (dataset, station_id, stop_id, decision, note)"
                " VALUES (%s, %s, %s, %s, %s)",
                [(NAME, p.station_id, p.stop_id, p.decision, p.note) for p in pairs.pairs],
            )
    return True
