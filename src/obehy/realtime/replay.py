"""``obehy rt replay``: resolve archived realtime payloads against a Parquet release.

archive → decode → episodes → trips/runs of the release → ``report.json`` + ``episodes.parquet``.
The output depends only on the archive slice and the release, so two runs are byte-identical.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.decode import ArrivaRow, DecodeStats, DukRow, SzRow, read_rows
from obehy.realtime.episodes import Episode, split_episodes
from obehy.realtime.release_index import PackageIndex, ReleaseIndexError
from obehy.realtime.resolve import (
    Resolution,
    collapse_repeats,
    resolve_arriva,
    resolve_duk,
    resolve_sz,
)

REPORT_SCHEMA_VERSION = 1
SOURCES = ("duk", "sz-mapa", "arriva-express")
JDF_NAMESPACES = ("cis:line", "cis:line_trip")
CZPTT_NAMESPACES = ("czptt:tr", "czptt:train_number")

Report = Callable[[str], None]


class ReplayError(RuntimeError):
    """The replay inputs cannot be read."""


@dataclass(frozen=True)
class ReplayOptions:
    release: Path
    archive: Path
    start: date
    end: date
    channels: Sequence[tuple[str, str]]
    out: Path


@dataclass
class _Output:
    rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    coverage: dict[str, dict[str, Counter[str]]] = field(
        default_factory=dict[str, dict[str, Counter[str]]]
    )

    def add(
        self,
        source: str,
        fleet: str,
        running: bool,
        episode: Episode[Any],
        resolution: Resolution,
        fields: dict[str, Any],
        repeat_of: str | None = None,
    ) -> None:
        bucket = self.coverage.setdefault(source, {}).setdefault(
            f"{fleet}/{'running' if running else 'not-running'}", Counter()
        )
        bucket["episodes"] += 1
        bucket[resolution.status] += 1
        if resolution.chosen is not None:
            bucket[f"method:{resolution.method}"] += 1
        chosen = resolution.chosen
        self.rows.append(
            {
                "source": source,
                "fleet": fleet,
                "vehicle": episode.vehicle,
                "episode": episode.number,
                "line": fields.get("line"),
                "trip_number": fields.get("trip_number"),
                "destination": fields.get("destination"),
                "start_local": episode.start,
                "end_local": episode.end,
                "polls": len(episode.rows),
                "running": running,
                "status": resolution.status,
                "method": resolution.method,
                "candidates": resolution.candidates,
                "matches": len(resolution.matches),
                "trip_id": chosen.trip_id if chosen else None,
                "run_key": chosen.run_key if chosen else None,
                "operating_date": chosen.operating_date if chosen else None,
                "score_min": chosen.score if chosen else None,
                "repeat_of": repeat_of,
            }
        )


_SCHEMA = pa.schema(
    [
        ("source", pa.string()),
        ("fleet", pa.string()),
        ("vehicle", pa.string()),
        ("episode", pa.int32()),
        ("line", pa.string()),
        ("trip_number", pa.string()),
        ("destination", pa.string()),
        ("start_local", pa.timestamp("us")),
        ("end_local", pa.timestamp("us")),
        ("polls", pa.int32()),
        ("running", pa.bool_()),
        ("status", pa.string()),
        ("method", pa.string()),
        ("candidates", pa.int32()),
        ("matches", pa.int32()),
        ("trip_id", pa.string()),
        ("run_key", pa.string()),
        ("operating_date", pa.date32()),
        ("score_min", pa.float64()),
        ("repeat_of", pa.string()),
    ]
)


def _replay_duk(rows: list[DukRow], jdf: PackageIndex, czptt: PackageIndex, out: _Output) -> None:
    episodes = split_episodes(
        rows,
        vehicle=lambda row: str(row.vehicle),
        key=lambda row: (row.cis_line, row.trip_number),
        time=lambda row: row.local,
    )
    resolved = [(episode, resolve_duk(jdf, czptt, episode)) for episode in episodes]
    repeats = collapse_repeats(jdf, [item for item in resolved if item[0].rows[0].fleet != "train"])
    for episode, resolution in resolved:
        first = episode.rows[0]
        out.add(
            "duk",
            first.fleet,
            any(row.state in (0, 1) for row in episode.rows),
            episode,
            resolution,
            {"line": str(first.cis_line), "trip_number": str(first.trip_number)},
            repeats.get(episode.episode_id),
        )


def _replay_sz(rows: list[SzRow], czptt: PackageIndex, out: _Output) -> None:
    episodes = split_episodes(
        rows,
        vehicle=lambda row: row.train_id,
        key=lambda _: (),
        time=lambda row: row.local,
        gap=None,
    )
    for episode in episodes:
        first = episode.rows[0]
        out.add(
            "sz-mapa",
            "replacement" if any(row.replacement for row in episode.rows) else "train",
            True,
            episode,
            resolve_sz(czptt, episode),
            {"line": first.category, "trip_number": first.train_number},
        )


def _replay_arriva(rows: list[ArrivaRow], jdf: PackageIndex, out: _Output) -> None:
    episodes = split_episodes(
        rows,
        vehicle=lambda row: row.plate,
        key=lambda row: (row.line, row.destination),
        time=lambda row: row.local,
    )
    for episode in episodes:
        first = episode.rows[0]
        out.add(
            "arriva-express",
            "express",
            True,
            episode,
            resolve_arriva(jdf, episode),
            {"line": first.line, "destination": first.destination},
        )


def _release(release: Path) -> dict[str, Any]:
    try:
        value = json.loads((release / "release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReplayError(f"Cannot read {release / 'release.json'}") from error
    if not isinstance(value, dict) or not isinstance(
        cast(dict[str, Any], value).get("run_id"), str
    ):
        raise ReplayError(f"Unsupported release.json in {release}")
    return cast(dict[str, Any], value)


def replay(options: ReplayOptions, report: Report = print) -> dict[str, Any]:
    release = _release(options.release)
    sources = {source for source, _ in options.channels}
    try:
        jdf = (
            PackageIndex(options.release / "jdf", JDF_NAMESPACES)
            if sources & {"duk", "arriva-express"}
            else None
        )
        czptt = (
            PackageIndex(options.release / "czptt", CZPTT_NAMESPACES)
            if sources & {"duk", "sz-mapa"}
            else None
        )
    except ReleaseIndexError as error:
        raise ReplayError(str(error)) from error
    report(f"release {release['run_id']} indexed")

    out = _Output()
    archive: dict[str, dict[str, object]] = {}
    for source, channel in sorted(options.channels):
        stats = DecodeStats()
        rows = list(read_rows(options.archive, source, channel, options.start, options.end, stats))
        if source == "duk":
            assert jdf is not None and czptt is not None
            _replay_duk(rows, jdf, czptt, out)
        elif source == "sz-mapa":
            assert czptt is not None
            _replay_sz(rows, czptt, out)
        elif source == "arriva-express":
            assert jdf is not None
            _replay_arriva(rows, jdf, out)
        else:
            raise ReplayError(f"No replay decoder for source {source}")
        archive[f"{source}/{channel}"] = stats.as_dict()
        report(f"{source}/{channel}: {stats.polls} polls, {stats.rows} rows")

    document: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "release": {
            "run_id": release["run_id"],
            "feed_versions": {
                name: index.feed_version
                for name, index in (("jdf", jdf), ("czptt", czptt))
                if index is not None
            },
        },
        "archive": {
            "from": options.start.isoformat(),
            "to": options.end.isoformat(),
            "channels": archive,
        },
        "coverage": {
            source: {group: dict(sorted(counts.items())) for group, counts in sorted(by.items())}
            for source, by in sorted(out.coverage.items())
        },
    }
    options.out.mkdir(parents=True, exist_ok=True)
    with atomic_output_path(options.out / "report.json") as temporary:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    rows = sorted(out.rows, key=lambda row: (row["source"], row["vehicle"], row["episode"]))
    with atomic_output_path(options.out / "episodes.parquet") as temporary:
        pq.write_table(  # pyright: ignore[reportUnknownMemberType]
            pa.Table.from_pylist(rows, schema=_SCHEMA), temporary
        )
    return document


def summary_lines(document: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for source, groups in document["coverage"].items():
        for group, counts in groups.items():
            total = counts["episodes"]
            unique = counts.get("unique", 0)
            rest = ", ".join(
                f"{name} {count}"
                for name, count in counts.items()
                if name not in ("episodes", "unique") and not name.startswith("method:")
            )
            lines.append(
                f"{source:15} {group:24} {unique:6}/{total:<6} unique"
                f" ({unique / total:.1%}){'; ' + rest if rest else ''}"
            )
    return lines
