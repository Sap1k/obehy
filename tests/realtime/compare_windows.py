"""The pinned compare references of real replay windows (docs/RUST_PORT.md, R0 item 1).

Each window is one `obehy rt compare` run over a local raw archive and release (both
gitignored). Short windows are committed as `tests/realtime/compare/<name>-<level>.jsonl.gz`;
the full hours of the cleanup's replays are written to `data/compare/` (gitignored) with
`--full`, because an hour of PID or MPV net is hundreds of megabytes even compressed.

    uv run python -m tests.realtime.compare_windows write [--full] [names...]
    uv run python -m tests.realtime.compare_windows check [--full] [names...]
    uv run python -m tests.realtime.compare_windows args [--full] NAME LEVEL   # the CLI arguments

`args` prints the exact arguments, which is what the Rust binary is run with; its stream is then
diffed with `obehy rt compare-diff <reference> <candidate>`.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from obehy.cli import main as obehy

ROOT = Path(__file__).resolve().parents[2]
PINNED = Path(__file__).resolve().parent / "compare"
FULL = ROOT / "data" / "compare"
LEVELS = ("facts", "output")


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    sources: str
    archive: str  # relative to the repository
    release: str
    day: str
    since: str  # Prague wall time
    until: str  # the committed window's end
    full_until: str  # the full window's end

    def args(self, level: str, full: bool, out: Path) -> list[str]:
        until = self.full_until if full else self.until
        argv = [
            "rt",
            "compare",
            "--level",
            level,
            "--archive",
            str(ROOT / self.archive),
            "--sources",
            self.sources,
            "--from",
            self.day,
            "--to",
            self.day,
            "--since",
            f"{self.day}T{self.since}",
            "--until",
            f"{self.day}T{until}",
            "--out",
            str(out),
        ]
        if level == "output":
            argv += ["--from-files", str(ROOT / "data" / "releases" / self.release)]
        return argv


R14 = "20261014T065758Z-d732b1d05827"
R15 = "20261015T014009Z-f4453d48b8d2"
WINDOWS = (
    Window("pid", "pid", "data/rt-raw-prod", R15, "2026-10-15", "07:00", "07:01", "08:00"),
    Window(
        "duk-sz-arriva",
        "duk,sz-mapa,arriva-express",
        "../rt-data/rt-raw",
        R14,
        "2026-10-06",
        "07:00",
        "07:03",
        "08:00",
    ),
    Window("dpmd", "dpmd", "data/rt-raw", R14, "2026-10-12", "07:00", "07:30", "08:00"),
    Window("mpvnet", "mpvnet", "data/rt-raw", R14, "2026-10-12", "07:00", "07:02", "08:00"),
)


def reference(window: Window, level: str, full: bool) -> Path:
    return (FULL if full else PINNED) / f"{window.name}-{level}.jsonl.gz"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["write", "check", "args"])
    parser.add_argument("--full", action="store_true", help="the full hours, into data/compare")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    full: bool = args.full
    if args.command == "args":
        name, level = args.names
        window = next(w for w in WINDOWS if w.name == name)
        print(" ".join(window.args(level, full, reference(window, level, full))))
        return
    chosen = [w for w in WINDOWS if not args.names or w.name in args.names]
    failed = 0
    for window in chosen:
        for level in LEVELS:
            target = reference(window, level, full)
            out = target if args.command == "write" else target.with_name("check-" + target.name)
            argv = window.args(level, full, out)
            if args.command == "check":
                argv += ["--check", str(target)]
            started = time.perf_counter()
            code = obehy(argv)
            print(f"{target.name}: {time.perf_counter() - started:.0f} s, exit {code}")
            failed += int(code != 0)
            if args.command == "check":
                out.unlink(missing_ok=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
