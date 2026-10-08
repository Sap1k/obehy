"""Archived realtime payloads → flat per-vehicle rows (connector-level normalisation only).

The quirks handled here are the ones established in `docs/sources/<source>.md`; no trip is
resolved and nothing is inferred.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from obehy.realtime.archive import iter_polls

PRAGUE = ZoneInfo("Europe/Prague")


class PayloadError(ValueError):
    """A successful poll whose body does not have the source's documented shape."""


def local_time(instant: datetime) -> datetime:
    """Naive Europe/Prague wall-clock time of an aware instant."""

    return instant.astimezone(PRAGUE).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class DukRow:
    received_at: datetime
    local: datetime
    vehicle: int
    fleet: str
    cis_line: int
    trip_number: int
    state: int
    delay: int | None
    gps_at: datetime | None
    arrival_at: datetime | None


@dataclass(frozen=True, slots=True)
class SzRow:
    received_at: datetime
    local: datetime
    train_id: str
    tr_key: str
    operating_date: date
    train_number: str
    category: str
    replacement: bool
    delay: int | None


@dataclass(frozen=True, slots=True)
class ArrivaRow:
    received_at: datetime
    local: datetime
    plate: str
    line: str
    destination: str
    next_stop: str
    delay: int | None
    at_stop: bool


Row = DukRow | SzRow | ArrivaRow


def duk_fleet(vehicle: int, cis_line: int) -> str:
    if cis_line == 0:
        return "train"
    if 400000 <= vehicle <= 409999:
        return "teplice"
    if 300000 <= vehicle <= 309999:
        return "dpmul"
    return "duk"


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _duk_time(value: object, *, utc_mislabelled: bool = False) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.year <= 1970:
        return None
    if utc_mislabelled:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def decode_duk(body: bytes, received_at: datetime) -> list[DukRow]:
    document = json.loads(body)
    if not isinstance(document, dict) or not isinstance(
        cast(dict[str, Any], document).get("VehicleList"), list
    ):
        raise PayloadError("DÚK payload lacks VehicleList")
    local = local_time(received_at)
    rows: list[DukRow] = []
    for entry in cast(list[dict[str, Any]], document["VehicleList"]):
        state = int(entry["State"])
        if state == 255:
            continue
        vehicle = int(entry["ID"])
        cis_line = int(entry["CISLineID"])
        fleet = duk_fleet(vehicle, cis_line)
        rows.append(
            DukRow(
                received_at=received_at,
                local=local,
                vehicle=vehicle,
                fleet=fleet,
                cis_line=cis_line,
                trip_number=int(entry["RouteID"]),
                state=state,
                delay=_optional_int(entry.get("Delay")),
                # Teplice units send UTC labelled +02:00 (duk.md).
                gps_at=_duk_time(entry.get("GPSPositionDT"), utc_mislabelled=fleet == "teplice"),
                arrival_at=_duk_time(entry.get("ArrivalDT")),
            )
        )
    return rows


def sz_tr_key(train_id: str) -> tuple[str, date]:
    """`TR/<company>/<core>/<variant>/<year>/<yyyyMMdd>` → (`Tr:…` key, operating date)."""

    parts = train_id.split("/")
    if len(parts) != 6 or parts[0] != "TR":
        raise PayloadError(f"Unexpected SŽ train id {train_id!r}")
    return f"Tr:{parts[1]}:{parts[2]}:{parts[3]}:{parts[4]}", datetime.strptime(
        parts[5], "%Y%m%d"
    ).date()


def decode_sz(body: bytes, received_at: datetime) -> list[SzRow]:
    document = json.loads(body)
    if not isinstance(document, dict) or not isinstance(
        cast(dict[str, Any], document).get("result"), list
    ):
        raise PayloadError("SŽ payload lacks result")
    local = local_time(received_at)
    rows: list[SzRow] = []
    for feature in cast(list[dict[str, Any]], document["result"]):
        properties = cast(dict[str, Any], feature["properties"])
        train_id = cast(str, properties["id"])
        tr_key, operating_date = sz_tr_key(train_id)
        rows.append(
            SzRow(
                received_at=received_at,
                local=local,
                train_id=train_id,
                tr_key=tr_key,
                operating_date=operating_date,
                train_number=str(properties.get("tn") or ""),
                category=str(properties.get("tt") or ""),
                replacement=_optional_int(properties.get("s")) == 1,
                delay=_optional_int(properties.get("de")),
            )
        )
    return rows


def decode_arriva(body: bytes, received_at: datetime) -> list[ArrivaRow]:
    document: Any = json.loads(body)
    if isinstance(document, list):
        document = cast(list[Any], document)[0] if document else None
    data = cast(dict[str, Any], cast(dict[str, Any], document or {}).get("data") or {})
    entries = data.get("busesCurrentLocations")
    if not isinstance(entries, list):
        raise PayloadError("Arriva payload lacks busesCurrentLocations")
    rows: list[ArrivaRow] = []
    for entry in cast(list[dict[str, Any]], entries):
        updated = cast(str, entry["updated"])
        rows.append(
            ArrivaRow(
                received_at=received_at,
                # `updated` is local time labelled +00:00 (arriva-express.md): keep the digits.
                local=datetime.fromisoformat(updated).replace(tzinfo=None, microsecond=0),
                plate=str(entry.get("spz") or "").strip(),
                line=str(entry.get("linkNumber") or ""),
                destination=str(entry.get("destinationName") or ""),
                next_stop=str(entry.get("lastStopName") or ""),
                delay=_optional_int(entry.get("delay")),
                at_stop=entry.get("state") == "v zastávce",
            )
        )
    return rows


Decoder = Callable[[bytes, datetime], list[Any]]

DECODERS: dict[str, Decoder] = {
    "duk": decode_duk,
    "sz-mapa": decode_sz,
    "arriva-express": decode_arriva,
}


@dataclass
class DecodeStats:
    polls: int = 0
    failed_polls: int = 0
    duplicate_payloads: int = 0
    bad_payloads: int = 0
    rows: int = 0
    errors: list[str] = field(default_factory=list[str])

    def as_dict(self) -> dict[str, object]:
        return {
            "polls": self.polls,
            "failed_polls": self.failed_polls,
            "duplicate_payloads": self.duplicate_payloads,
            "bad_payloads": self.bad_payloads,
            "rows": self.rows,
            "first_errors": self.errors[:5],
        }


def read_rows(
    root: Path,
    source: str,
    channel: str,
    start: date,
    end: date,
    stats: DecodeStats,
) -> Iterator[Any]:
    """Decoded rows of every distinct successful payload, in recording order."""

    decoder = DECODERS[source]
    seen: set[str] = set()
    for poll in iter_polls(root, source, channel, start, end):
        stats.polls += 1
        sha256 = cast(str | None, poll.entry.get("sha256"))
        status = cast(int | None, poll.entry.get("status"))
        if poll.entry.get("error") or sha256 is None or status is None or not 200 <= status < 300:
            stats.failed_polls += 1
            continue
        if sha256 in seen:
            stats.duplicate_payloads += 1
            continue
        seen.add(sha256)
        body = poll.body()
        assert body is not None
        try:
            rows = decoder(body, poll.received_at.astimezone(UTC))
        except (ValueError, KeyError, TypeError, IndexError) as error:
            stats.bad_payloads += 1
            stats.errors.append(f"{poll.entry.get('received_at')}: {error}")
            continue
        stats.rows += len(rows)
        yield from rows
