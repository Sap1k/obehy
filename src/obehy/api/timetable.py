"""A line's printed timetable laid out: its trips split into directions, and each direction's
stops in one order every trip reads down (`/lines/{route_id}/timetable`). Pure: rows in, rows
out."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from itertools import pairwise

# A row of a direction: a stop, and which visit of it (a loop calls at its terminus twice).
Row = tuple[str, int]


def rows_of(stops: Sequence[str]) -> list[Row]:
    """A trip's stops as rows: the first visit of each stop is 1, a later one 2 and on."""

    seen: Counter[str] = Counter()
    rows: list[Row] = []
    for stop in stops:
        seen[stop] += 1
        rows.append((stop, seen[stop]))
    return rows


def merge(trips: Sequence[Sequence[Row]]) -> list[Row]:
    """One order of the rows of all `trips` that keeps each trip's own order where the trips
    agree: the most common sequence (the longest on a tie) is the backbone, and each other
    trip's rows not yet placed go in after the row that comes before them in that trip. Short
    turns and branches fit in; a trip that disagrees with the order keeps its rows where the
    others put them."""

    counts = Counter(tuple(t) for t in trips)
    ordered = sorted(counts, key=lambda t: (-counts[t], -len(t), t))
    order: list[Row] = []
    for sequence in ordered:
        at = -1  # the place of the trip's row placed last
        for row in sequence:
            if row in order:
                at = order.index(row)
            else:
                at += 1
                order.insert(at, row)
    return order


def reversed_against(backbone: Sequence[Row], trip: Sequence[Row]) -> bool:
    """Whether `trip` runs against `backbone` more than with it, by the stops they share."""

    place = {stop: n for n, (stop, _) in enumerate(backbone)}
    shared = [place[stop] for stop, _ in trip if stop in place]
    pairs = list(pairwise(shared))
    return sum(b < a for a, b in pairs) > sum(b > a for a, b in pairs)


def directions(trips: Sequence[tuple[int | None, Sequence[Row]]]) -> list[list[int]]:
    """The trips (by index) of each direction: by the feed's direction where it has one; trips
    without one split by whether they run with or against the most common of them."""

    groups: dict[tuple[int | None, bool], list[int]] = {}
    unknown = [n for n, (direction, _) in enumerate(trips) if direction is None]
    counts = Counter(tuple(trips[n][1]) for n in unknown)
    backbone = max(counts, key=lambda t: (counts[t], len(t), t)) if counts else ()
    for n, (direction, rows) in enumerate(trips):
        against = direction is None and reversed_against(backbone, rows)
        groups.setdefault((direction, against), []).append(n)
    return [groups[key] for key in sorted(groups, key=lambda k: (k[0] is None, k[0] or 0, k[1]))]
