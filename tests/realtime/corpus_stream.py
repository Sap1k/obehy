"""The output-level compare stream (`obehy rt compare --level output`) of each corpus journey
(`tests/realtime/corpus.py`), pinned in `tests/golden/corpus-stream/<name>.jsonl.gz` as the
reference a port of the core is compared on journey by journey.

Each journey runs as `corpus.run` runs it: its observations in `(received_at, source, item)`
order, a tick every `emit.tick_s` on round multiples from the first observation until 35 min
after the last, each tick refreshing and then estimating. The stream, in the canonical form
of `realtime/canonical.py`:

- `header`: `corpus` (the journey's name), `feed`, `core_version`, `policy_version`;
- `physical_vehicles` rows (the writer opening, `at` null);
- per step and per tick, its `effect`s (`phase` `step` | `tick`) then the writer's rows of them
  (`emit/recording.py`; derivation release `corpus`);
- on every tick that is a multiple of `STATE_EVERY_S`, the state rows and the feed's `gtfs_rt`;
- at the end, the state rows once more.

    uv run python -m tests.realtime.corpus_stream write    # rewrite every journey's stream
    uv run python -m tests.realtime.corpus_stream check    # first difference per journey
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from obehy.realtime.canonical import line
from obehy.realtime.compare import gtfs_rt
from obehy.realtime.compare_check import diff
from obehy.realtime.core import CORE_VERSION
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.recording import RecordingStore
from obehy.realtime.jsonl import open_lines
from obehy.realtime.model import Derivation, Effect, FeedState
from obehy.realtime.times import Instant
from tests.realtime.corpus import POLICY, load, names, run

STREAMS = Path(__file__).resolve().parents[1] / "golden" / "corpus-stream"
STATE_EVERY_S = 60


def stream(name: str) -> list[str]:
    """One journey's stream, as canonical lines."""

    index, _ = load(name)
    lines = [
        line(
            {
                "kind": "header",
                "corpus": name,
                "feed": index.feed,
                "core_version": CORE_VERSION,
                "policy_version": POLICY.version,
            }
        )
    ]
    store = RecordingStore()
    writer = Writer(store, Derivation(CORE_VERSION, POLICY.version, "corpus"))

    def flush(at: Instant | None) -> None:
        lines.extend(line(record | {"at": at}) for record in store.take())

    def stepped(phase: str, at: Instant, effects: list[Effect]) -> None:
        lines.extend(
            line({"kind": "effect", "at": at, "phase": phase, "effect": e}) for e in effects
        )
        if effects:
            writer.write(effects)
        flush(at)

    last: list[tuple[FeedState, Instant]] = []

    def tick(state: FeedState, now: Instant) -> None:
        last[:] = [(state, now)]
        if int(now.timestamp()) % STATE_EVERY_S == 0:
            writer.write_state({state.feed: state})
            flush(now)
            lines.append(line(gtfs_rt(state, now, POLICY, writer)))

    flush(None)
    run(name, tick=tick, stepped=stepped)
    state, now = last[0]
    writer.write_state({state.feed: state})
    flush(now)
    return lines


def path(name: str) -> Path:
    return STREAMS / f"{name}.jsonl.gz"


def write(name: str) -> None:
    STREAMS.mkdir(parents=True, exist_ok=True)
    with open_lines(path(name), "w") as out:
        out.writelines(stream(name))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["write", "check"])
    parser.add_argument("names", nargs="*", help="journeys (default: all)")
    args = parser.parse_args()
    chosen: list[str] = args.names or names()
    if args.command == "write":
        wanted = set(names())
        for stale in STREAMS.glob("*.jsonl.gz"):
            if stale.name.removesuffix(".jsonl.gz") not in wanted:
                stale.unlink()
        for name in chosen:
            write(name)
        return
    failed = 0
    scratch = STREAMS.parent / ".corpus-stream-check.jsonl.gz"
    for name in chosen:
        with open_lines(scratch, "w") as out:
            out.writelines(stream(name))
        same, report = diff(path(name), scratch)
        if not same:
            failed += 1
            print(f"== {name}\n" + "\n".join(report))
    scratch.unlink(missing_ok=True)
    print(f"{len(chosen) - failed}/{len(chosen)} journeys give their pinned stream")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
