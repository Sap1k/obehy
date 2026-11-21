"""`obehy rt compare-diff` and `obehy rt compare --check`: where two compare streams part.

Both streams are read record by record in order (`.gz` or plain). The report says how many
records of each kind (`kind`, with `/table` for state rows) each side has, then the first
`limit` positions whose records differ: the line number, the kind, the first differing path
inside the record and both values, and the two lines (shortened). `ignore` takes `KIND.FIELD`
(a top-level field of records of that kind, e.g. `payload_error.message`) to leave out of the
comparison. Same streams: exit code 0; different: 1.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Sequence
from itertools import zip_longest
from pathlib import Path
from typing import Any, cast

from obehy.realtime.jsonl import open_lines

SHOWN = 400  # characters of each differing line shown


def _records(path: Path) -> Iterator[str]:
    with open_lines(path, "r") as stream:
        for text in stream:
            yield text.rstrip("\n")


def kind_of(record: dict[str, Any]) -> str:
    kind = str(record.get("kind"))
    table = record.get("table")
    return f"{kind}/{table}" if isinstance(table, str) else kind


def _parse(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    return cast(dict[str, Any], json.loads(text))


def _strip(record: dict[str, Any] | None, ignore: set[tuple[str, str]]) -> dict[str, Any] | None:
    if record is None:
        return None
    kind = str(record.get("kind"))
    return {k: v for k, v in record.items() if (kind, k) not in ignore}


def first_difference(a: Any, b: Any, path: str = "") -> tuple[str, Any, Any] | None:
    """The first path (`.key` / `[i]`) at which two JSON values differ, with both values."""

    x: object = a
    y: object = b
    if isinstance(x, dict) and isinstance(y, dict):
        left, right = cast(dict[str, object], x), cast(dict[str, object], y)
        for key in sorted(left.keys() | right.keys()):
            found = first_difference(left.get(key), right.get(key), f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(x, list) and isinstance(y, list):
        left_items, right_items = cast(list[object], x), cast(list[object], y)
        for i, (u, v) in enumerate(zip_longest(left_items, right_items)):
            found = first_difference(u, v, f"{path}[{i}]")
            if found is not None:
                return found
        return None
    return None if _same(a, b) else (path or ".", a, b)


def _same(a: object, b: object) -> bool:
    return a == b and a.__class__ is b.__class__


def _short(text: str | None) -> str:
    if text is None:
        return "(end of stream)"
    return text if len(text) <= SHOWN else text[:SHOWN] + "..."


def diff(
    reference: Path, candidate: Path, *, limit: int = 10, ignore: Sequence[str] = ()
) -> tuple[bool, list[str]]:
    """Whether the streams are the same (ignoring `ignore`), and the report's lines."""

    ignored = {(i.split(".", 1)[0], i.split(".", 1)[1]) for i in ignore if "." in i}
    kinds: dict[str, Counter[str]] = {"reference": Counter(), "candidate": Counter()}
    differing: Counter[str] = Counter()
    shown: list[str] = []
    total = 0
    for number, (a_text, b_text) in enumerate(
        zip_longest(_records(reference), _records(candidate)), start=1
    ):
        a, b = _parse(a_text), _parse(b_text)
        if a is not None:
            kinds["reference"][kind_of(a)] += 1
        if b is not None:
            kinds["candidate"][kind_of(b)] += 1
        if a_text == b_text:
            continue
        a_cmp, b_cmp = _strip(a, ignored), _strip(b, ignored)
        if a_cmp == b_cmp and a_cmp is not None:
            continue
        total += 1
        kind = kind_of(a if a is not None else cast(dict[str, Any], b))
        differing[kind] += 1
        if len(shown) // 4 < limit:
            where = first_difference(a_cmp, b_cmp) if a_cmp and b_cmp else None
            detail = "" if where is None else f" at {where[0]}: {where[1]!r} != {where[2]!r}"
            shown += [
                f"line {number} ({kind}){detail}",
                f"  reference: {_short(a_text)}",
                f"  candidate: {_short(b_text)}",
                "",
            ]
    lines = ["records by kind (reference / candidate):"]
    for kind in sorted(kinds["reference"].keys() | kinds["candidate"].keys()):
        r, c = kinds["reference"][kind], kinds["candidate"][kind]
        mark = "" if r == c else "   <-- differs"
        lines.append(f"  {kind}: {r} / {c}{mark}")
    if not total:
        lines.append("streams are the same")
        return True, lines
    lines.append(
        f"{total} differing positions: " + ", ".join(f"{k} {n}" for k, n in differing.most_common())
    )
    lines.append(f"first {min(limit, total)}:")
    return False, lines + shown
