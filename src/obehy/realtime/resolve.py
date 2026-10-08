"""Resolve realtime episodes to trips (or CZPTT runs) of a release.

Pure functions of an episode and the release indexes; the rules are the ones replayed in the
source dossiers (`docs/sources/<source>.md`, "Matching (replayed)").
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise

from obehy.realtime.decode import ArrivaRow, DukRow, SzRow
from obehy.realtime.episodes import Episode
from obehy.realtime.release_index import PackageIndex

UNIQUE = "unique"
AMBIGUOUS = "ambiguous"
WRONG_TIME = "wrong_time"
NOT_ACTIVE = "not_active"
NO_TRIP = "no_trip"
NO_LINE = "no_line"
UNSCORED = "unscored"

KEYED_MARGIN_S = 3600
ARRIVA_MARGIN_S = 1800
ARRIVA_BEST_MAX_MIN = 2.0
ARRIVA_RUNNER_UP_MIN = 10.0
REPEAT_MARGIN_S = 600


@dataclass(frozen=True, slots=True)
class Match:
    trip_id: str
    run_key: str | None
    operating_date: date
    score: float | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str
    method: str
    candidates: int
    matches: tuple[Match, ...] = ()

    @property
    def chosen(self) -> Match | None:
        return self.matches[0] if self.status == UNIQUE else None


def operating_dates(start: datetime) -> tuple[date, date]:
    """A trip seen at local time `start` runs on that date or, past midnight, the day before."""

    return start.date(), start.date() - timedelta(days=1)


def seconds_after_midnight(moment: datetime, day: date) -> float:
    return (moment - datetime.combine(day, time())).total_seconds()


def _overlaps(
    span: tuple[int, int] | None, start: datetime, end: datetime, day: date, margin_s: int
) -> bool:
    if span is None:
        return False
    return (
        seconds_after_midnight(end, day) >= span[0] - margin_s
        and seconds_after_midnight(start, day) <= span[1] + margin_s
    )


def resolve_keyed(
    index: PackageIndex,
    namespace: str,
    identifier: str,
    start: datetime,
    end: datetime,
    *,
    per_run: bool = False,
    line: tuple[str, str] | None = None,
    method: str = "",
) -> Resolution:
    """Trips with the key valid and running on the operating date whose span (±1 h) overlaps.

    With `per_run`, trip parts of one CZPTT run count once and the run's span is used.
    """

    entries = index.keys(namespace, identifier)
    candidates: dict[tuple[str, date], Match] = {}
    for day in operating_dates(start):
        for entry in entries:
            if not entry.valid_on(day) or not index.runs_on(entry.public_id, day):
                continue
            trip = index.trip(entry.public_id)
            run_key = (trip.run_key if trip else None) or entry.public_id
            target = run_key if per_run else entry.public_id
            candidates.setdefault((target, day), Match(entry.public_id, run_key, day))
    good = [
        match
        for (target, day), match in sorted(candidates.items())
        if _overlaps(
            index.run_span(target) if per_run else index.span(target),
            start,
            end,
            day,
            KEYED_MARGIN_S,
        )
    ]
    method = method or namespace
    if len(good) == 1:
        return Resolution(UNIQUE, method, len(candidates), tuple(good))
    if good:
        return Resolution(AMBIGUOUS, method, len(candidates), tuple(good))
    if candidates:
        status = WRONG_TIME
    elif entries:
        status = NOT_ACTIVE
    elif line is not None and not index.keys(*line):
        status = NO_LINE
    else:
        status = NO_TRIP
    return Resolution(status, method, len(candidates))


def resolve_duk(jdf: PackageIndex, czptt: PackageIndex, episode: Episode[DukRow]) -> Resolution:
    row = episode.rows[0]
    if row.fleet == "train":
        return resolve_keyed(
            czptt,
            "czptt:train_number",
            str(row.trip_number),
            episode.start,
            episode.end,
            per_run=True,
        )
    line = f"{row.cis_line:06d}"
    return resolve_keyed(
        jdf,
        "cis:line_trip",
        f"{line}:{row.trip_number}",
        episode.start,
        episode.end,
        line=("cis:line", line),
    )


def _runs_on(index: PackageIndex, namespace: str, identifier: str, day: date) -> list[Match]:
    runs: dict[str, Match] = {}
    for entry in index.keys(namespace, identifier):
        if entry.valid_on(day) and index.runs_on(entry.public_id, day):
            trip = index.trip(entry.public_id)
            run_key = (trip.run_key if trip else None) or entry.public_id
            runs.setdefault(run_key, Match(entry.public_id, run_key, day))
    return [runs[run_key] for run_key in sorted(runs)]


def resolve_sz(czptt: PackageIndex, episode: Episode[SzRow]) -> Resolution:
    """The TR key on the operating date in the train id; the train number if the TR is ambiguous."""

    row = episode.rows[0]
    runs = _runs_on(czptt, "czptt:tr", row.tr_key, row.operating_date)
    if len(runs) == 1:
        return Resolution(UNIQUE, "czptt:tr", 1, tuple(runs))
    if runs:
        by_number = _runs_on(czptt, "czptt:train_number", row.train_number, row.operating_date)
        if len(by_number) == 1:
            return Resolution(UNIQUE, "czptt:train_number", len(runs), tuple(by_number))
        return Resolution(AMBIGUOUS, "czptt:tr", len(runs), tuple(runs))
    status = NOT_ACTIVE if czptt.keys("czptt:tr", row.tr_key) else NO_TRIP
    return Resolution(status, "czptt:tr", 0)


def normalise_stop_name(name: str) -> str:
    """`Brno, Benešova tř.,hotel GRAND` and `Brno,Benešova tř.hotel GRAND` compare equal."""

    return name.replace(",", "").replace(" ", "").lower()


def resolve_arriva(jdf: PackageIndex, episode: Episode[ArrivaRow]) -> Resolution:
    """Line + destination + time candidates, scored at the departures `lastStopName` reveals.

    `lastStopName` is the next stop (arriva-express.md): when it first shows a stop, the bus
    has just left the stop before it, so `updated - delay` is compared with the candidate's
    scheduled departure from the preceding call. The score is the median absolute residual.
    """

    first = episode.rows[0]
    destination = normalise_stop_name(first.destination)
    entries = jdf.keys_with_prefix("cis:line_trip", first.line)
    running: set[tuple[str, date]] = set()
    for day in operating_dates(episode.start):
        for entry in entries:
            if entry.valid_on(day) and jdf.runs_on(entry.public_id, day):
                running.add((entry.public_id, day))
    calls = jdf.calls({trip_id for trip_id, _ in running})
    candidates: list[tuple[str, date]] = []
    for trip_id, day in sorted(running):
        trip_calls = calls[trip_id]
        times = [call.time for call in trip_calls if call.time is not None]
        if (
            trip_calls
            and times
            and normalise_stop_name(trip_calls[-1].stop_name) == destination
            and _overlaps(
                (min(times), max(times)), episode.start, episode.end, day, ARRIVA_MARGIN_S
            )
        ):
            candidates.append((trip_id, day))
    if not candidates:
        return Resolution(NO_TRIP, "line+destination", 0)

    departures: dict[str, ArrivaRow] = {}
    for row in episode.rows:
        if row.next_stop and row.delay is not None:
            departures.setdefault(normalise_stop_name(row.next_stop), row)
    scored: list[Match] = []
    for trip_id, day in candidates:
        trip_calls = calls[trip_id]
        residuals: list[float] = []
        for previous, call in pairwise(trip_calls):
            row = departures.get(normalise_stop_name(call.stop_name))
            if row is None or previous.time is None or row.delay is None:
                continue
            departed = seconds_after_midnight(row.local - timedelta(minutes=row.delay), day)
            residuals.append(abs(departed - previous.time) / 60)
        if residuals:
            trip = jdf.trip(trip_id)
            scored.append(
                Match(trip_id, trip.run_key if trip else None, day, statistics.median(residuals))
            )
    if not scored:
        return Resolution(UNSCORED, "line+destination", len(candidates))
    scored.sort(key=lambda match: (match.score, match.trip_id, match.operating_date))
    best = scored[0].score
    runner_up = scored[1].score if len(scored) > 1 else None
    assert best is not None
    confident = best <= ARRIVA_BEST_MAX_MIN and (
        runner_up is None or runner_up >= ARRIVA_RUNNER_UP_MIN
    )
    return Resolution(
        UNIQUE if confident else AMBIGUOUS,
        "line+destination+time",
        len(candidates),
        tuple(scored),
    )


def collapse_repeats(
    index: PackageIndex,
    resolved: Sequence[tuple[Episode[DukRow], Resolution]],
) -> dict[str, str]:
    """Stale keys (duk.md): a vehicle's repeated episodes of one trip collapse to the episode
    that best overlaps the schedule (±10 min). Returns `episode_id → kept episode_id`."""

    groups: dict[tuple[str, str, date], list[tuple[float, Episode[DukRow]]]] = defaultdict(list)
    for episode, resolution in resolved:
        chosen = resolution.chosen
        span = index.span(chosen.trip_id) if chosen else None
        if chosen is None or span is None:
            continue
        day = chosen.operating_date
        overlap = min(seconds_after_midnight(episode.end, day), span[1] + REPEAT_MARGIN_S) - max(
            seconds_after_midnight(episode.start, day), span[0] - REPEAT_MARGIN_S
        )
        groups[(episode.vehicle, chosen.trip_id, day)].append((overlap, episode))
    repeats: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        kept = max(members, key=lambda member: (member[0], -member[1].number))[1]
        for _, episode in members:
            if episode is not kept:
                repeats[episode.episode_id] = kept.episode_id
    return repeats
