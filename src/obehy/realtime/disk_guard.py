"""What the disk guard deletes next when the data disk runs short (`obehy jobs hourly`).

The oldest raw archive day goes first (history can no longer be rebuilt from it, but nothing
shown is lost), then the oldest history day; neither below its policy minimum. One step at a
time: the caller deletes, measures again and asks again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from obehy.realtime.policy import GuardPolicy

GB = 1024**3


@dataclass(frozen=True, slots=True)
class Step:
    kind: Literal["raw", "history"]
    day: date


def next_step(
    free_bytes: int, guard: GuardPolicy, raw_days: Iterable[date], history_days: Iterable[date]
) -> Step | None:
    """The next day to delete, or None when enough is free or nothing more may go."""

    if free_bytes >= guard.free_gb * GB:
        return None
    raw = sorted(set(raw_days))
    if len(raw) > guard.min_raw_days:
        return Step("raw", raw[0])
    history = sorted(set(history_days))
    if len(history) > guard.min_history_days:
        return Step("history", history[0])
    return None
