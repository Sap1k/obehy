"""``obehy release feed-list``: the public list of the active release's feeds.

nginx serves each static feed under a stable name straight from ``releases/active``
(``deploy/nginx.conf``), so the URLs never change; this writes the page that lists them, with
sizes and the release they come from, as ``index.html`` and ``feeds.json``.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from obehy.pipeline.files import atomic_output_path


@dataclass(frozen=True)
class Feed:
    name: str  # the published file name, served next to the list
    source: str  # path inside the release directory
    title: str


# The names the MOTIS node's /get-feeds/ directory used, so its URLs keep working when it points
# here.
STATIC_FEEDS = (
    Feed("cz-jdf-gtfs.zip", "jdf/gtfs.zip", "Buses (JDF), national"),
    Feed("cz-jdf-filtered-gtfs.zip", "jdf-filtered/gtfs.zip", "Buses (JDF), filtered"),
    Feed("cz-czptt-gtfs.zip", "czptt/gtfs.zip", "Rail (CZPTT), national"),
)
REALTIME_FEEDS = (Feed("cz-jdf-gtfs-rt.pb", "", "Buses (JDF), GTFS-RT, updated every 10 s"),)


def _built(completed_at: object) -> str:
    """The build time in Prague, as people read it; the raw value if it is not a timestamp."""

    try:
        when = datetime.fromisoformat(str(completed_at))
    except ValueError:
        return str(completed_at)
    local = when.astimezone(ZoneInfo("Europe/Prague"))
    return f"{local.day}. {local.month}. {local.year} {local:%H:%M}"


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    raise AssertionError("unreachable")


def feed_list(release_dir: Path) -> dict[str, Any]:
    release = cast(
        dict[str, Any], json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
    )
    feeds: list[dict[str, Any]] = []
    for feed in STATIC_FEEDS:
        path = release_dir / feed.source
        if path.is_file():
            stat = path.stat()
            feeds.append(
                {
                    "name": feed.name,
                    "title": feed.title,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(
                        timespec="seconds"
                    ),
                }
            )
    feeds.extend({"name": feed.name, "title": feed.title} for feed in REALTIME_FEEDS)
    return {
        "run_id": release["run_id"],
        "completed_at": release.get("completed_at"),
        "feeds": feeds,
    }


def _html(listing: dict[str, Any]) -> str:
    rows = "\n".join(
        '<tr><td><a href="{href}">{name}</a></td><td>{title}</td><td>{size}</td></tr>'.format(
            href=html.escape(feed["name"], quote=True),
            name=html.escape(feed["name"]),
            title=html.escape(feed["title"]),
            size=_size(feed["size_bytes"]) if "size_bytes" in feed else "live",
        )
        for feed in listing["feeds"]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oběhy feeds</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 44rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ padding: .35rem .6rem; border-bottom: 1px solid #8884; text-align: left; }}
td:last-child, th:last-child {{ text-align: right; white-space: nowrap; }}
small {{ opacity: .7; }}
</style>
</head>
<body>
<h1>Oběhy feeds</h1>
<p><small>Release {html.escape(listing["run_id"])}, built {
        html.escape(_built(listing["completed_at"]))
    }. File names stay the same across releases. Machine-readable:
<a href="feeds.json">feeds.json</a>.</small></p>
<table>
<thead><tr><th>File</th><th>Feed</th><th>Size</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""


def write_feed_list(release_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Write ``index.html`` and ``feeds.json`` for the release into ``out_dir``."""

    listing = feed_list(release_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with atomic_output_path(out_dir / "feeds.json") as path:
        path.write_text(json.dumps(listing, indent=2) + "\n", encoding="utf-8")
    with atomic_output_path(out_dir / "index.html") as path:
        path.write_text(_html(listing), encoding="utf-8")
    return listing
