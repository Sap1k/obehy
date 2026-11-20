"""The realtime processes' log line: a UTC timestamp to the second and the message, on stdout,
flushed (the worker, the recorder, the lookup broker and egress all log alike)."""

from __future__ import annotations

from obehy.realtime.times import now


def log(message: str) -> None:
    print(f"{now().isoformat(timespec='seconds')} {message}", flush=True)
