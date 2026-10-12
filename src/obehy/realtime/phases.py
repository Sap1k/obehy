"""Time per pipeline phase (archive, decode, core, writes, emit), for the worker's periodic
summary and `obehy rt bench`.

Each phase gets thread CPU time and wall time: the CPU is what the worker's own thread spent
(its fetches run on other threads), the wall time also counts waiting on the database, which
blocks the event loop just the same.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager


class Phases:
    __slots__ = ("calls", "cpu_s", "wall_s")

    def __init__(self) -> None:
        self.cpu_s: dict[str, float] = {}
        self.wall_s: dict[str, float] = {}
        self.calls: dict[str, int] = {}

    @contextmanager
    def time(self, name: str) -> Generator[None]:
        cpu, wall = time.thread_time(), time.perf_counter()
        try:
            yield
        finally:
            self.cpu_s[name] = self.cpu_s.get(name, 0.0) + time.thread_time() - cpu
            self.wall_s[name] = self.wall_s.get(name, 0.0) + time.perf_counter() - wall
            self.calls[name] = self.calls.get(name, 0) + 1

    def line(self, over_s: float) -> str:
        """Each phase's CPU and wall time as a share of one core over `over_s` seconds."""

        cpu, wall = sum(self.cpu_s.values()), sum(self.wall_s.values())
        parts = [
            f"{name} {100 * self.cpu_s[name] / over_s:.1f}/{100 * spent / over_s:.1f}%"
            for name, spent in sorted(self.wall_s.items(), key=lambda item: -item[1])
        ]
        return (
            f"cpu/wall {100 * cpu / over_s:.1f}/{100 * wall / over_s:.1f}% of a core: "
            + ", ".join(parts)
        )

    def reset(self) -> None:
        self.cpu_s.clear()
        self.wall_s.clear()
        self.calls.clear()
