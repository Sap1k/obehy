"""A keyless vehicle's episode: what it has shown since its line or destination last changed.

An episode lasts while the line and destination stay the same and the vehicle is heard at least
every `episode_gap_s`. Each change of its next-stop name is a departure from the stop named
until then (ARRIVA-Q2), at the observation's time; with the source's delay it implies the
scheduled departure, an interval when the delay is whole minutes rounded down (ARRIVA-Q3). The
names are the source's; some are not stops of the timetable (a motorway junction, ARRIVA-Q8).
"""

from __future__ import annotations

from datetime import timedelta

from obehy.realtime.model import (
    Delay,
    Departure,
    Destination,
    Interval,
    KeylessEpisode,
    LineRef,
    NextStopName,
    Observation,
    Position,
    SourceSemantics,
)
from obehy.realtime.policy import KeylessPolicy

MAX_DEPARTURES = 40  # longer than any trip's stop list; older ones no longer decide anything


def same_name(a: str, b: str) -> bool:
    """Stop names compared without commas, spaces and case (ARRIVA-Q6: `Brno, Benešova
    tř.,hotel GRAND` is `Brno,Benešova tř.hotel GRAND`)."""

    return normal(a) == normal(b)


def normal(name: str) -> str:
    return name.replace(",", "").replace(" ", "").casefold()


def advance(
    previous: KeylessEpisode | None,
    observation: Observation,
    semantics: SourceSemantics,
    policy: KeylessPolicy,
) -> KeylessEpisode | None:
    """The episode after `observation`; None when it names no line or destination."""

    line, destination = observation.first(LineRef), observation.first(Destination)
    if line is None or destination is None:
        return None
    at = observation.at
    following = observation.first(NextStopName)
    name = following.name if following is not None else None
    if (
        previous is None
        or previous.line != line.value
        or not same_name(previous.destination, destination.name)
        or at - previous.last_at > timedelta(seconds=policy.episode_gap_s)
    ):
        return KeylessEpisode(line.value, destination.name, at, at, name)
    if at < previous.last_at:
        return previous  # an older fix arriving late tells nothing new
    departures = previous.departures
    if (
        name is not None
        and previous.next_stop is not None
        and not same_name(name, previous.next_stop)
        and observation.observed_at is not None
    ):
        left = Departure(
            previous.next_stop,
            at,
            _scheduled(observation, semantics),
            observation.first(Position),
        )
        departures = (*departures, left)[-MAX_DEPARTURES:]
    return KeylessEpisode(
        previous.line,
        previous.destination,
        previous.started_at,
        at,
        name if name is not None else previous.next_stop,
        departures,
    )


def _scheduled(observation: Observation, semantics: SourceSemantics) -> Interval | None:
    """When the timetable had the departure the vehicle just made: its time minus the delay."""

    delay = observation.first(Delay)
    if delay is None:
        return None
    hi = observation.at - timedelta(seconds=delay.seconds)
    if semantics.delay_floor_minute:  # d minutes means d to d + 59 s late
        return Interval(hi - timedelta(seconds=59), hi)
    return Interval(hi, hi)
