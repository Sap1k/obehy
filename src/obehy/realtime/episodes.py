"""Group a vehicle's decoded rows into episodes: runs of polls reporting the same key."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

EPISODE_GAP = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Episode[R]:
    vehicle: str
    number: int
    key: Hashable
    rows: tuple[R, ...]
    start: datetime
    end: datetime

    @property
    def episode_id(self) -> str:
        return f"{self.vehicle}#{self.number}"


def split_episodes[R](
    rows: Iterable[R],
    *,
    vehicle: Callable[[R], str],
    key: Callable[[R], Hashable],
    time: Callable[[R], datetime],
    gap: timedelta | None = EPISODE_GAP,
) -> list[Episode[R]]:
    """Episodes ordered by vehicle and start.

    A vehicle's rows (stably sorted by `time`) form one episode while `key` stays equal and,
    when `gap` is set, consecutive rows are no more than `gap` apart.
    """

    by_vehicle: dict[str, list[R]] = defaultdict(list)
    for row in rows:
        by_vehicle[vehicle(row)].append(row)
    episodes: list[Episode[R]] = []
    for name in sorted(by_vehicle):
        groups: list[list[R]] = []
        for row in sorted(by_vehicle[name], key=time):
            last = groups[-1][-1] if groups else None
            if (
                last is None
                or key(row) != key(last)
                or (gap is not None and time(row) - time(last) > gap)
            ):
                groups.append([])
            groups[-1].append(row)
        for number, group in enumerate(groups):
            episodes.append(
                Episode(name, number, key(group[0]), tuple(group), time(group[0]), time(group[-1]))
            )
    return episodes
