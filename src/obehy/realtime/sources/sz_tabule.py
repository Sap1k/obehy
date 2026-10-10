"""SŽ station board connector: `InfoTabule` HTML → platforms (docs/sources/sz-tabule.md).

One poll is one station's board: about 20 arrivals and 20 departures. Each row with a platform
becomes a vehicle-less observation, keyed by train number, carrying a `PlatformAssignment` for
the station (the request's SR70, kept in the archive's index line). Quirks normalized here:
SZT-Q1 (the 6-digit request code loses its check digit), SZT-Q2 (the last column's header says
whether values are platforms or tracks), SZT-Q4 (`-` and `ND` are no assignment), SZT-Q5 (bare
`HH:mm` scheduled times, resolved near reception). Every value is shown twice (desktop and
mobile cells); only desktop cells are read, by their column class.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from html.parser import HTMLParser
from typing import Any, Literal

from obehy.realtime.model import (
    Fact,
    Observation,
    PlatformAssignment,
    PlatformLabel,
    RawRef,
    TripKey,
)
from obehy.realtime.times import Instant, resolve_clock

SOURCE = "sz-tabule"
CHANNEL = "board"
DECODER_VERSION = 1

TABLES: dict[str, Literal["arrival", "departure"]] = {"prijezdy": "arrival", "odjezdy": "departure"}
LABELS: dict[str, PlatformLabel] = {"nástupiště": "platform", "kolej": "track"}
UNASSIGNED = frozenset({"", "-", "ND"})  # SZT-Q4
HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
COLUMN = re.compile(r"^inTaCol[PO]-\d+$")


class PayloadError(ValueError):
    """A successful poll whose body does not have the documented shape."""


@dataclass(slots=True)
class _Cell:
    column: str | None
    mobile: bool
    text: str = ""
    train: str | None = None


@dataclass(slots=True)
class _Table:
    kind: Literal["arrival", "departure"]
    header: list[tuple[str | None, str]] = field(default_factory=list[tuple[str | None, str]])
    rows: list[list[_Cell]] = field(default_factory=list[list[_Cell]])


class _Board(HTMLParser):
    """Collects the two tables: header cells and row cells with their column class."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self._table: _Table | None = None
        self._cell: _Cell | None = None
        self._head = False
        self._row: list[_Cell] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = values.get("class", "").split()
        if tag == "div" and values.get("data-typ") in TABLES:
            self._table = _Table(TABLES[values["data-typ"]])
            self.tables.append(self._table)
        elif self._table is None:
            return
        elif tag == "thead":
            self._head = True
        elif tag == "tbody":
            self._head = False
        elif tag == "tr" and not self._head:
            self._row = []
            self._table.rows.append(self._row)
        elif tag in ("td", "th"):
            column = next((c for c in classes if COLUMN.match(c)), None)
            self._cell = _Cell(column, "d-md-none" in classes)
            if "data-trainnumber" in values:
                self._cell.train = values["data-trainnumber"]

    def handle_endtag(self, tag: str) -> None:
        if tag not in ("td", "th") or self._cell is None or self._table is None:
            return
        cell, self._cell = self._cell, None
        cell.text = " ".join(cell.text.split())
        if tag == "th" and self._head:
            if not cell.mobile:
                self._table.header.append((cell.column, cell.text))
        elif self._row is not None:
            self._row.append(cell)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text += data


def code5(station: str) -> str | None:
    """The 6-digit request code without its check digit (SZT-Q1)."""

    return station[:5] if len(station) == 6 and station.isdigit() else None


def decode(
    body: bytes,
    sha256: str,
    received_at: Instant,
    max_clock_skew: timedelta,
    request: Mapping[str, Any],
) -> list[Observation]:
    del max_clock_skew  # the board has no clock of its own
    station = code5(str(request.get("sr70", "")))
    if station is None:
        raise PayloadError("poll without the station's SR70 code")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PayloadError(f"not UTF-8: {error}") from error
    board = _Board()
    board.feed(text)
    board.close()
    if not board.tables:
        raise PayloadError("no arrival or departure table")
    observations: list[Observation] = []
    item = 0
    for table in board.tables:
        label = _label(table)
        scheduled_column = next(
            (column for column, name in table.header if name == "Pravidelný"), None
        )
        platform_column = table.header[-1][0] if table.header else None
        if label is None or scheduled_column is None or platform_column is None:
            continue
        for row in table.rows:
            fact = _row(
                row, table.kind, station, label, scheduled_column, platform_column, received_at
            )
            if fact is not None:
                key, assignment = fact
                observations.append(
                    Observation(
                        source=SOURCE,
                        channel=CHANNEL,
                        feed="czptt",
                        received_at=received_at,
                        observed_at=None,
                        raw=RawRef(sha256, item),
                        decoder_version=DECODER_VERSION,
                        facts=(key, assignment),
                    )
                )
            item += 1
    return observations


def _label(table: _Table) -> PlatformLabel | None:
    if not table.header:
        return None
    return LABELS.get(table.header[-1][1].strip().lower())


def _row(
    row: list[_Cell],
    kind: Literal["arrival", "departure"],
    station: str,
    label: PlatformLabel,
    scheduled_column: str,
    platform_column: str,
    received_at: Instant,
) -> tuple[Fact, PlatformAssignment] | None:
    desktop = [cell for cell in row if not cell.mobile]
    train = next((cell.train for cell in row if cell.train), None)
    scheduled = next((c.text for c in desktop if c.column == scheduled_column), None)
    value = next((c.text for c in desktop if c.column == platform_column), None)
    if train is None or not train.isdigit() or scheduled is None:
        return None
    if value is None or value in UNASSIGNED:
        return None
    match = HHMM.match(scheduled)
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    when = resolve_clock(time(hours, minutes), received_at)
    if when is None:
        return None
    return TripKey("czptt:train_number", train), PlatformAssignment(
        station, kind, when, value, label
    )
