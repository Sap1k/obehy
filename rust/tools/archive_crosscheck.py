"""The Rust raw archive against the Python one (docs/RUST_PORT.md, R8).

    uv run python rust/tools/archive_crosscheck.py dump --root ROOT --source S --channel C \
        --from 2026-10-05 --to 2026-10-05 --out OUT.jsonl
    uv run python rust/tools/archive_crosscheck.py write --root ROOT
    uv run python rust/tools/archive_crosscheck.py same-tree A B

`dump` reads a window with `archive.iter_polls` and writes one canonical line per poll, in
order: the parsed `received_at`, the index entry, and the payload's sha256 and length (the
bytes `ArchivedPoll.body()` returns). The Rust side writes the same lines:

    cargo run --release -p obehy-rt --example archive_check -- dump ROOT S C FROM TO OUT.jsonl

`write` appends a fixed set of polls with `ArchiveWriter` (errors, filter fields, a demand
request, non-ASCII text, a repeated payload, a UTC midnight); `archive_check write` appends the
same polls from Rust. `same-tree` compares two archive trees file for file, byte for byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from obehy.realtime.archive import ArchiveWriter, Poll, iter_polls

T0 = datetime(2026, 10, 5, 23, 59, 50, 250_000, tzinfo=UTC)
DATE = "Mon, 05 Oct 2026 21:59:50 GMT"


def polls() -> list[tuple[str, str, Poll, bytes | None, dict[str, object]]]:
    """(source, channel, poll, stored body, extra): mirrored in examples/archive_check.rs."""

    def at(ms: int) -> datetime:
        return T0 + timedelta(milliseconds=ms)

    payload = '{"VehicleList":[{"ID":"Čáslav ü"}]}'.encode()
    return [
        (
            "duk",
            "vehicles",
            Poll(at(0), at(120), 200, payload, "application/json", {"date": DATE, "age": "3"}),
            payload,
            {},
        ),
        (
            "duk",
            "vehicles",
            Poll(at(15000), at(15120), 200, payload, "application/json", {"date": DATE}),
            payload,
            {},
        ),
        (
            "duk",
            "vehicles",
            Poll(at(30000), at(50000), None, None, error="TimeoutError: timed out"),
            None,
            {},
        ),
        (
            "duk",
            "vehicles",
            Poll(at(45000), at(45001), 502, b"bad gateway", "text/html", error="HTTP 502 Bad Gateway"),
            b"bad gateway",
            {},
        ),
        (
            "arriva-express",
            "buses",
            Poll(at(1000), at(1500), 200, b"[1,2,3]", "application/json"),
            b"[1]",
            {
                "filter": "arriva-express@1",
                "source_bytes": 7,
                "source_sha256": hashlib.sha256(b"[1,2,3]").hexdigest(),
                "kept": 1,
                "dropped": 2,
            },
        ),
        (
            "arriva-express",
            "buses",
            Poll(at(2000), at(2500), 200, b"{}", None),
            b"{}",
            {"filter": "arriva-express@1", "filter_error": 'Expected a "GraphQL" batch\n\t\x01'},
        ),
        (
            "sz-tabule",
            "board",
            Poll(at(3000), at(3333), 200, b"<html>\xc5\xbd</html>", "text/html; charset=UTF-8"),
            b"<html>\xc5\xbd</html>",
            {"request": {"sr70": "534149"}},
        ),
    ]


def write(root: Path) -> None:
    writer = ArchiveWriter(root)
    for source, channel, poll, body, extra in polls():
        writer.append(source, channel, poll, body, extra)


def dump(root: Path, source: str, channel: str, start: date, end: date, out: Path) -> int:
    count = 0
    with out.open("w", encoding="utf-8", newline="\n") as stream:
        for number, poll in enumerate(iter_polls(root, source, channel, start, end)):
            body = poll.body()
            line: dict[str, Any] = {
                "n": number,
                "received_at": poll.received_at.isoformat(),
                "entry": poll.entry,
                "body_sha256": None if body is None else hashlib.sha256(body).hexdigest(),
                "body_bytes": None if body is None else len(body),
            }
            stream.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    return count


def same_tree(a: Path, b: Path) -> bool:
    def files(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    left, right = files(a), files(b)
    same = True
    for name in sorted(set(left) | set(right)):
        if left.get(name) != right.get(name):
            print(f"differs: {name}", file=sys.stderr)
            same = False
    print(f"{len(left)} files" + (", identical" if same else ", DIFFERENT"))
    return same


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    dumper = commands.add_parser("dump")
    dumper.add_argument("--root", type=Path, required=True)
    dumper.add_argument("--source", required=True)
    dumper.add_argument("--channel", required=True)
    dumper.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    dumper.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    dumper.add_argument("--out", type=Path, required=True)
    writer = commands.add_parser("write")
    writer.add_argument("--root", type=Path, required=True)
    trees = commands.add_parser("same-tree")
    trees.add_argument("a", type=Path)
    trees.add_argument("b", type=Path)
    args = parser.parse_args()
    if args.command == "dump":
        count = dump(args.root, args.source, args.channel, args.start, args.end, args.out)
        print(f"{count} polls")
        return 0
    if args.command == "write":
        write(args.root)
        return 0
    return 0 if same_tree(args.a, args.b) else 1


if __name__ == "__main__":
    sys.exit(main())
