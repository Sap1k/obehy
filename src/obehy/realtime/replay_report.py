"""`report.json` (and `keyless.jsonl`) of a replay, read from its output stream: the records of
`obehy-rt compare --level output --diagnostics` (their shapes are in the module docs of
`rust/obehy-rt/src/compare.rs`), fed one at a time in stream order. `obehy rt report
--from-stream` reads them from a file. Nothing here reads the core.

The report:

- `archive.channels`: per channel the polls, failed polls, undecodable payloads and observations
  (`poll` records); `skipped_other_feeds`: observations of feeds not replayed;
- `results`: observations by feed and outcome, and per DÚK fleet (per source otherwise) the
  share of running key groups (vehicle, trip key, local day) that bound to a journey
  (`observation` rows);
- with the rail feed, `unresolved` (names that could not be placed: SŽ points, board rows; the
  steps' `Unresolved` effects) and the shadow error of each rail predictor (`evaluate.py`);
- `keyless` (`keyless_report.py`) when a keyless source was replayed; `quality` (`quality.py`)
  when the stream has `journey_sample` records.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.evaluate import PredictorLog
from obehy.realtime.journey_view import JourneyView
from obehy.realtime.jsonl import open_lines
from obehy.realtime.keyless_report import KeylessLog, first_facts, observed
from obehy.realtime.quality import QualityLog
from obehy.realtime_config import local_date, parse_utc

REPORT_SCHEMA_VERSION = 2
Record = dict[str, Any]
Group = tuple[str, str, tuple[str, str], date]


class ReportError(ValueError):
    """The stream cannot make a report (not an output stream with the diagnostics records)."""


@dataclass(slots=True)
class _Stats:
    polls: int = 0
    failed_polls: int = 0
    bad_payloads: int = 0
    observations: int = 0


class ReplayReport:
    """The report of one stream: `add` each record after the `header`, then `document`."""

    def __init__(self, header: Record, episode_gap_s: int) -> None:
        if header.get("kind") != "header" or header.get("level") != "output":
            raise ReportError("not an output-level compare stream")
        if not header.get("diagnostics"):
            raise ReportError("the stream has no diagnostics records (compare --diagnostics)")
        self.header = header
        self.feeds = cast(list[str], header["feeds"])
        self.stats: dict[str, _Stats] = defaultdict(_Stats)
        self.skipped = 0
        self.groups: dict[Group, bool] = {}
        self.reasons: Counter[str] = Counter()
        self.unresolved: Counter[str] = Counter()
        self.keyless = KeylessLog(episode_gap_s)
        self.predictors = PredictorLog() if "czptt" in self.feeds else None
        self.quality = QualityLog() if header.get("quality") else None
        self.view = JourneyView()

    def add(self, record: Record) -> None:
        kind = record["kind"]
        if kind == "observation":
            self._observation(record["row"])
        elif kind == "effect":
            effect = record["effect"]
            if record["phase"] == "step" and effect["@type"] == "Unresolved":
                self.unresolved[f"{effect['observation']['source']}/{effect['kind']}"] += 1
        elif kind == "poll":
            self._poll(record)
        elif kind == "predictor_sample" and self.predictors is not None:
            self.predictors.sample(record)
        elif kind == "journey_sample" and self.quality is not None:
            self.quality.sample(record["feed"], self.view.read(record), parse_utc(record["at"]))

    def _poll(self, record: Record) -> None:
        stats = self.stats[f"{record['source']}/{record['channel']}"]
        stats.polls += 1
        outcome = record["outcome"]
        if outcome == "failed":
            stats.failed_polls += 1
        elif outcome == "undecodable":
            stats.bad_payloads += 1
        else:
            stats.observations += record["observations"]
            feeds = cast(dict[str, int], record["feeds"])
            self.skipped += sum(n for feed, n in feeds.items() if feed not in self.feeds)

    def _observation(self, row: Record) -> None:
        self.keyless.observe(row)
        bound = row["journey_key"] is not None
        self.reasons[f"{row['feed']}/{'bound' if bound else row['reason']}"] += 1
        facts = first_facts(row)
        vehicle, key = facts.get("vehicle_key"), facts.get("trip_key")
        state = facts.get("source_state")
        if vehicle is None or key is None or (state is not None and state["code"] != "running"):
            return
        group = (row["source"], vehicle["id"], (key["ns"], key["key"]), local_date(observed(row)))
        self.groups[group] = self.groups.get(group, False) or bound

    def document(self) -> dict[str, Any]:
        header = self.header
        document: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "core_version": header["core_version"],
            "release": {"run_id": header["release"]},
            "archive": {
                "from": header["from"],
                "to": header["to"],
                "channels": {
                    name: {
                        "polls": s.polls,
                        "failed_polls": s.failed_polls,
                        "bad_payloads": s.bad_payloads,
                        "observations": s.observations,
                    }
                    for name, s in sorted(self.stats.items())
                },
            },
            "feeds": self.feeds,
            "skipped_other_feeds": self.skipped,
            "results": dict(sorted(self.reasons.items())),
            "running_key_groups": _fleets(self.groups),
        }
        if self.predictors is not None:
            document["unresolved"] = dict(sorted(self.unresolved.items()))
            document["predictors"] = self.predictors.report()
        if self.quality is not None:
            document["quality"] = self.quality.report()
        if self.keyless.groups:
            document["keyless"] = self.keyless.report()
        return document

    def write(self, out: Path) -> dict[str, Any]:
        """`report.json` into `out`, and `keyless.jsonl` when a keyless source was replayed."""

        document = self.document()
        out.mkdir(parents=True, exist_ok=True)
        if self.keyless.groups:
            with atomic_output_path(out / "keyless.jsonl") as temporary:
                self.keyless.write(temporary)
        with atomic_output_path(out / "report.json") as temporary:
            temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
        return document


# The fleets the bound shares are counted by: DÚK's own buses, Teplice and Ústí city buses and
# its trains (docs/sources/duk.md), MPV net's buses and trains, PID's modes.
_DUK_TEPLICE = range(400000, 410000)
_DUK_DPMUL = range(300000, 310000)
_PID_MODES = {
    0: "tram",
    1: "metro",
    2: "train",
    3: "bus",
    4: "ferry",
    7: "funicular",
    11: "trolleybus",
}


def fleet(source: str, vehicle: str, feed: str) -> str:
    """The fleet a key group's vehicle counts in: `<source>/<fleet>`, or the source."""

    if source == "duk":
        if feed == "czptt":
            return "duk/train"
        number = int(vehicle) if vehicle.isdigit() else -1
        if number in _DUK_TEPLICE:
            return "duk/teplice"
        return "duk/dpmul" if number in _DUK_DPMUL else "duk/duk"
    if source == "mpvnet":
        return "mpvnet/train" if feed == "czptt" else "mpvnet/bus"
    if source == "pid":
        if feed == "czptt":
            return "pid/train"
        head = vehicle.split(":", 1)[0]
        if head == "trip":
            return "pid/metro"
        return f"pid/{_PID_MODES.get(int(head), 'other') if head.isdigit() else 'other'}"
    return source


def _fleets(groups: dict[Group, bool]) -> dict[str, dict[str, int]]:
    fleets: dict[str, Counter[str]] = defaultdict(Counter)
    for (source, vehicle, (namespace, _), _), bound in groups.items():
        name = fleet(source, vehicle, "czptt" if namespace.startswith("czptt:") else "jdf")
        fleets[name]["key_groups"] += 1
        fleets[name]["bound"] += int(bound)
    return {name: dict(sorted(counter.items())) for name, counter in sorted(fleets.items())}


def records(path: Path) -> Iterable[Record]:
    """The records of a stream file (`.gz` or plain)."""

    with open_lines(path, "r") as stream:
        for text in stream:
            yield cast(Record, json.loads(text))


def from_stream(path: Path, out: Path, episode_gap_s: int) -> dict[str, Any]:
    """`report.json` (and `keyless.jsonl`) of a stream file, written into `out`."""

    stream = iter(records(path))
    head = next(stream, None)
    if head is None:
        raise ReportError(f"{path} is empty")
    report = ReplayReport(head, episode_gap_s)
    for record in stream:
        report.add(record)
    return report.write(out)


def summary_lines(document: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for name, counter in document["running_key_groups"].items():
        total, bound = counter.get("key_groups", 0), counter.get("bound", 0)
        share = 100 * bound / total if total else 0.0
        lines.append(f"{name}: {bound}/{total} running key groups bound ({share:.1f}%)")
    return lines
