"""The compiler invocation of the static build: one `jrutil build` over a written specification.

The specification is the interface of jrutil-rs (`docs/switch.md` and `crates/jrutil/src/spec.rs`
of its checkout). It names every input as downloaded and every output; the compiler reads
nothing else and refuses to leave an invalid package.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from obehy.pipeline.args import JobSetting
from obehy.pipeline.process import CommandFn
from obehy.pipeline.reporting import CommandProgress, Reporter
from obehy.route_shapes import RouteShapes

SPEC_FILE = "build.toml"


@dataclass(frozen=True)
class Posts:
    """Boarding-post estimation (`[jdf.posts]`)."""

    routing: Path
    policy: Path
    reports: Path
    cache: Path | None


@dataclass(frozen=True)
class Filtered:
    """The filtered road feed (`[jdf.filtered]`)."""

    rules: Path
    output: Path


@dataclass(frozen=True)
class BuildSpec:
    """Every input and output of one build."""

    timetable_year: int
    # Rail.
    czptt_snapshot: Path
    railway_osm: Path
    czptt_output: Path
    # Road.
    catalogues: Path
    gazetteer: Path
    post_candidates_osm: Path
    route_rules: Path
    reference_date: date
    jdf_output: Path
    vld_archive: Path
    drahy_archive: Path
    overlay_reports: Path
    pid_feed: Path
    pid_overrides: Path
    ids_jmk_feed: Path
    registry: Path | None = None
    registry_candidates: Path | None = None
    posts: Posts | None = None
    filtered: Filtered | None = None
    shapes: RouteShapes | None = None


def _value(value: Path | str | int) -> str:
    if isinstance(value, int):
        return str(value)
    text = value.absolute().as_posix() if isinstance(value, Path) else value
    # A JSON string is a valid TOML basic string.
    return json.dumps(text, ensure_ascii=False)


def _table(header: str, entries: Sequence[tuple[str, Path | str | int | None]]) -> list[str]:
    lines = [header]
    lines += [f"{key} = {_value(value)}" for key, value in entries if value is not None]
    return [*lines, ""]


def _shapes(header: str, shapes: RouteShapes | None) -> list[str]:
    if shapes is None:
        return []
    return _table(
        header,
        [("companion", shapes.companion), ("graph", shapes.graph), ("cache", shapes.cache)],
    )


def spec_text(spec: BuildSpec) -> str:
    """The TOML specification of `spec`, with absolute forward-slash paths."""

    lines = _table(
        "[czptt]",
        [
            ("snapshot", spec.czptt_snapshot),
            ("osm", spec.railway_osm),
            ("timetable_year", spec.timetable_year),
            ("output", spec.czptt_output),
        ],
    )
    lines += _shapes("[czptt.shapes]", spec.shapes)
    lines += _table(
        "[jdf]",
        [
            ("catalogues", spec.catalogues),
            ("gazetteer", spec.gazetteer),
            ("osm", spec.post_candidates_osm),
            ("registry", spec.registry),
            ("route_rules", spec.route_rules),
            ("timetable_year", spec.timetable_year),
            ("reference_date", spec.reference_date.isoformat()),
            ("output", spec.jdf_output),
            ("registry_candidates", spec.registry_candidates),
        ],
    )
    for name, archive in (("vld", spec.vld_archive), ("drahy", spec.drahy_archive)):
        lines += _table("[[jdf.exports]]", [("name", name), ("archive", archive)])
    if spec.posts is not None:
        lines += _table(
            "[jdf.posts]",
            [
                ("routing", spec.posts.routing),
                ("policy", spec.posts.policy),
                ("reports", spec.posts.reports),
                ("cache", spec.posts.cache),
            ],
        )
    lines += _table("[jdf.overlay]", [("reports", spec.overlay_reports)])
    lines += _table(
        "[[jdf.overlay.sources]]",
        [("profile", "pid"), ("feed", spec.pid_feed), ("overrides", spec.pid_overrides)],
    )
    lines += _table(
        "[[jdf.overlay.sources]]", [("profile", "ids-jmk"), ("feed", spec.ids_jmk_feed)]
    )
    lines += _shapes("[jdf.shapes]", spec.shapes)
    if spec.filtered is not None:
        lines += _table(
            "[jdf.filtered]", [("rules", spec.filtered.rules), ("output", spec.filtered.output)]
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def run(
    spec: BuildSpec,
    *,
    root: Path,
    compiler: Sequence[str],
    jobs: JobSetting,
    log: Path,
    reporter: Reporter,
    command_runner: CommandFn,
) -> Path:
    """Write `<root>/build.toml` and compile it; returns the specification's path."""

    path = root / SPEC_FILE
    path.write_text(spec_text(spec), encoding="utf-8", newline="\n")
    command_runner(
        [*compiler, "build", "--spec", str(path), "--progress-events"],
        root,
        log,
        reporter,
        CommandProgress("Compile packages"),
        None if jobs == "auto" else {"RAYON_NUM_THREADS": str(jobs)},
    )
    return path
