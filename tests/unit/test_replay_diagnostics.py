"""The diagnostics over the worker's output stream (`obehy-rt compare --level output --diagnostics
--quality`): `obehy rt report --from-stream` and the quality log and checks.
The streams are written here by hand in the shapes of `rust/obehy-rt/src/compare.rs`."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from obehy import cli
from obehy.realtime.journey_view import JourneyView
from obehy.realtime.quality import QualityLog
from obehy.realtime.replay_report import ReportError, from_stream
from obehy.realtime_config import PRAGUE, Instant, instant, utc_text

Record = dict[str, Any]
DAY = "2026-10-08"
JOURNEY = {"feed": "jdf", "namespace": "cis:line_trip", "key": "582492:143", "service_date": DAY}


def local(clock: str) -> Instant:
    return instant(datetime.fromisoformat(f"{DAY} {clock}").replace(tzinfo=PRAGUE))


def utc(clock: str | None) -> str | None:
    return None if clock is None else utc_text(local(clock))


def _call(
    planned: str,
    *,
    arrival: str | None = None,
    departure: str | None = None,
    estimate: str | None = None,
    passed_arrival: str | None = None,
    passed_departure: str | None = None,
    status: str = "predicted",
) -> Record:
    estimated = estimate or planned
    return {
        "arrival": utc(arrival),
        "departure": utc(departure),
        "estimated_arrival": utc(estimated),
        "estimated_departure": utc(estimated),
        "passed_arrival": utc(passed_arrival),
        "passed_departure": utc(passed_departure),
        "passenger": True,
        "status": status,
    }


@dataclass(slots=True)
class Run:
    """A bus on 582492:143 over A (0 m, 08:00), B (1000 m, 08:05) and C (2000 m, 08:10), as its
    `journey_sample` records state it: each sample names the call reached, the lead fix and the
    calls as the worker left them (every call each time; the plan on the first sample only)."""

    calls: list[Record] = field(
        default_factory=lambda: [_call("08:00:00"), _call("08:05:00"), _call("08:10:00")]
    )
    sampled: bool = False

    def sample(
        self,
        clock: str,
        reached: int,
        along_m: float,
        *,
        at_stop: bool = False,
        near: tuple[int, ...] = (),
        lifecycle: str = "running",
        lost: bool = False,
    ) -> Record:
        plan = None
        if not self.sampled:
            plan = {
                "scheduled": [[utc(t), utc(t)] for t in ("08:00:00", "08:05:00", "08:10:00")],
                "zones": [[None, 30.0], [970.0, 1030.0], [1970.0, None]],
                "calls_m": [0.0, 1000.0, 2000.0],
                "shaped": True,
                "points": 40,
            }
            self.sampled = True
        journey = {
            "journey": JOURNEY,
            "trip_id": "jdf:trip:582492:143",
            "mode": "bus",
            "lifecycle": lifecycle,
            "stale": lost,
            "lost": lost,
            "rail": False,
            "lead": True,
            "progress": {"call": reached, "along_m": along_m},
            "track": {
                "seen_at": utc(clock),
                "at_stop": at_stop,
                "lateness_s": 0.0,
                "hypotheses": True,
                "off_path": False,
            },
            "fix": {
                "best_m": 5.0,
                "first_m": along_m,
                "last_m": 2000.0 - along_m,
                "near": list(near),
            },
            "n": len(self.calls),
            "calls": [[i, call] for i, call in enumerate(self.calls)],
            "plan": plan,
        }
        return {"kind": "journey_sample", "at": utc(clock), "feed": "jdf", "journeys": [journey]}


def _ran(run: Run) -> list[Record]:
    """A plain run: half a minute late at first, on time from B, half a minute at B."""

    run.calls[1] = _call("08:05:00", estimate="08:05:30")
    run.calls[2] = _call("08:10:00", estimate="08:10:30")
    samples = [run.sample("08:00:00", 0, 0.0, at_stop=True, near=(0,))]
    run.calls[0] = _call(
        "08:00:00", departure="08:00:20", passed_departure="08:00:20", status="actual"
    )
    run.calls[1] = _call("08:05:00", estimate="08:05:20")
    run.calls[2] = _call("08:10:00", estimate="08:10:20")
    samples.append(run.sample("08:02:30", 0, 500.0))
    run.calls[1] = _call("08:05:00")
    run.calls[2] = _call("08:10:00")
    samples.append(run.sample("08:04:40", 0, 900.0))
    arrived = {"arrival": "08:05:00", "passed_arrival": "08:05:00", "status": "actual"}
    run.calls[1] = _call("08:05:00", **arrived)
    samples.append(run.sample("08:05:00", 1, 1000.0, at_stop=True, near=(1,)))
    samples.append(run.sample("08:05:30", 1, 1000.0, at_stop=True, near=(1,)))
    run.calls[1] = _call("08:05:00", **arrived, departure="08:05:30", passed_departure="08:05:30")
    run.calls[2] = _call("08:10:00", estimate="08:10:15")
    samples.append(run.sample("08:05:45", 1, 1100.0))
    run.calls[2] = _call("08:10:00")
    samples.append(run.sample("08:08:00", 1, 1600.0))
    run.calls[2] = _call("08:10:00", arrival="08:10:00", passed_arrival="08:10:00", status="actual")
    samples.append(run.sample("08:10:00", 2, 2000.0, at_stop=True, near=(2,)))
    return samples


def _quality(samples: list[Record]) -> dict[str, Any]:
    log, view = QualityLog(), JourneyView()
    for record in samples:
        log.sample(record["feed"], view.read(record), local_at(record["at"]))
    return log.report()


def local_at(text: str) -> Instant:
    return instant(datetime.fromisoformat(text))


def test_quality_scores_predictions_and_measures_the_dwell_at_a_stop_without_one() -> None:
    report = _quality(_ran(Run()))
    # Next call: B predicted 30, 20 and 0 s late, then C 0, 0, 15 and 0 s; both arrived on time.
    accuracy = report["accuracy"]["bus"]
    assert (accuracy["1"]["n"], accuracy["1"]["mae_s"], accuracy["1"]["bias_s"]) == (7, 9.3, 9.3)
    assert (accuracy["2"]["n"], accuracy["2"]["mae_s"]) == (3, 16.7)
    dwell = report["dwell"]["bus"]
    assert (dwell["n"], dwell["p"]["50"]) == (1, 30.0)
    # Each call's predicted delay fell by half a minute once.
    assert report["flicker"]["bus"] == {
        "calls": 2,
        "moves_per_call": 1.0,
        "reversals_per_call": 0.0,
    }
    checks = report["checks"]["events"]["bus"]
    assert checks["departures"] == 2
    assert checks["departed_while_there"] == checks["stood_without_dwell"] == 0


def test_checks_flag_a_departure_timed_while_the_fixes_are_still_at_the_stop() -> None:
    run = Run()
    samples = [run.sample("08:04:00", 0, 800.0)]
    # Arrived and left B at 08:05 by the worker's times; its fixes stay there until 08:06:30.
    at_b = {"arrival": "08:05:00", "passed_arrival": "08:05:00", "status": "actual"}
    run.calls[1] = _call("08:05:00", **at_b, departure="08:05:00", passed_departure="08:05:00")
    for clock in ("08:05:00", "08:05:45", "08:06:30"):
        samples.append(run.sample(clock, 1, 1000.0, at_stop=True, near=(1,)))
    samples.append(run.sample("08:06:45", 1, 1150.0))
    samples.append(run.sample("08:10:00", 1, 1700.0))
    checks = _quality(samples)["checks"]
    events = checks["events"]["bus"]
    assert events["departed_while_there"] == 1 and events["stood_without_dwell"] == 1
    assert checks["examples"]["stood_without_dwell"][0]["call"] == 1


def test_checks_count_a_journey_lost_just_short_of_its_last_stop() -> None:
    run = Run()
    samples = [
        run.sample(clock, 0, metres) for clock, metres in (("08:00:00", 0), ("08:03:00", 600))
    ]
    samples.append(run.sample("08:09:00", 1, 1700.0))
    samples.append(run.sample("08:45:00", 1, 1700.0, lost=True))
    report = _quality(samples)["checks"]
    events = report["events"]["bus"]
    assert events["ended"] == 1 and events["ended_with_time"] == 0
    assert events["lost_near_end"] == 1
    assert report["examples"]["lost_near_end"][0]["last_fix_from_end_m"] == 300


# --- the report ---------------------------------------------------------------------------------


def _poll(clock: str, outcome: str, observations: int = 0) -> Record:
    record: Record = {
        "kind": "poll",
        "at": utc(clock),
        "source": "duk",
        "channel": "vehicles",
        "outcome": outcome,
        "observations": observations,
    }
    if outcome == "decoded":
        record["feeds"] = {"jdf": observations}
    return record


def _observation(clock: str, vehicle: str, key: str, bound: bool) -> Record:
    return {
        "kind": "observation",
        "at": utc(clock),
        "row": {
            "source": "duk",
            "channel": "vehicles",
            "feed": "jdf",
            "facts": {
                "v": 5,
                "facts": [
                    {"t": "vehicle_key", "id": vehicle},
                    {"t": "trip_key", "ns": "cis:line_trip", "key": key},
                    {"t": "source_state", "code": "running"},
                ],
            },
            "observed_at": utc(clock),
            "received_at": utc(clock),
            "journey_key": key if bound else None,
            "service_date": DAY if bound else None,
            "reason": None if bound else "unknown_key",
        },
    }


def _stream(quality: bool = True) -> list[Record]:
    header: Record = {
        "kind": "header",
        "level": "output",
        "compare_version": 1,
        "core_version": "r1.0",
        "policy_version": "18",
        "release": "run-a",
        "from": DAY,
        "to": DAY,
        "feeds": ["jdf", "czptt"],
        "diagnostics": True,
    }
    if quality:
        header["quality"] = True
    records = [header]
    for minute in range(10):
        clock = f"08:{minute:02d}:00"
        if minute == 3:
            records.append(_poll(clock, "failed"))
            continue
        records.append(_poll(clock, "decoded", 2))
        records.append(_observation(clock, "810", "582492:143", bound=minute >= 1))
        records.append(_observation(clock, "811", "999999:7", bound=False))
    records.append(
        {
            "kind": "effect",
            "phase": "step",
            "at": utc("08:04:00"),
            "effect": {
                "@type": "Unresolved",
                "kind": "point",
                "observation": {"source": "sz-mapa"},
            },
        }
    )
    records += _ran(Run()) if quality else []
    return records


def _write(path: Path, records: list[Record]) -> Path:
    text = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_the_report_reads_polls_rows_effects_and_samples(tmp_path: Path) -> None:
    stream = _write(tmp_path / "stream.jsonl.gz", _stream())
    document = from_stream(stream, tmp_path / "report", 1800)

    assert json.loads((tmp_path / "report" / "report.json").read_text("utf-8")) == document
    assert document["release"] == {"run_id": "run-a"}
    channel = document["archive"]["channels"]["duk/vehicles"]
    assert (channel["polls"], channel["failed_polls"], channel["observations"]) == (10, 1, 18)
    assert document["results"] == {"jdf/bound": 8, "jdf/unknown_key": 10}
    # Two vehicles on their keys this day: bus 810 bound, 811's key unknown.
    assert document["running_key_groups"] == {"duk/duk": {"bound": 1, "key_groups": 2}}
    assert document["unresolved"] == {"sz-mapa/point": 1}
    assert document["predictors"]["sz"] == {"n": 0}
    assert document["quality"]["accuracy"]["bus"]["1"]["n"] == 7
    assert "keyless" not in document


def test_report_from_stream_on_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = _write(tmp_path / "stream.jsonl", _stream(quality=False))
    out = tmp_path / "report"
    assert cli.main(["rt", "report", "--from-stream", str(stream), "--out", str(out)]) == 0
    assert "duk/duk: 1/2 running key groups bound (50.0%)" in capsys.readouterr().out
    assert "quality" not in json.loads((out / "report.json").read_text("utf-8"))
    plain = _stream(quality=False)
    plain[0].pop("diagnostics")
    with pytest.raises(ReportError, match="no diagnostics"):
        from_stream(_write(tmp_path / "plain.jsonl", plain), out, 1800)
    assert (
        cli.main(
            ["rt", "report", "--from-stream", str(tmp_path / "plain.jsonl"), "--out", str(out)]
        )
        == 1
    )


def test_a_stream_gives_the_same_report_gzipped_or_not(tmp_path: Path) -> None:
    records = _stream()
    plain = from_stream(_write(tmp_path / "s.jsonl", records), tmp_path / "a", 1800)
    packed = from_stream(_write(tmp_path / "s.jsonl.gz", records), tmp_path / "b", 1800)
    assert plain == packed
    assert (tmp_path / "a" / "report.json").read_bytes() == (
        tmp_path / "b" / "report.json"
    ).read_bytes()
