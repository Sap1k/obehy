from __future__ import annotations

import json
from pathlib import Path

from obehy.release.feed_list import write_feed_list


def test_feed_list_names_every_feed_of_the_release(tmp_path: Path) -> None:
    release = tmp_path / "20261009T030215Z-8cf199243726"
    for package in ("jdf", "czptt"):  # no jdf-filtered in this release
        (release / package).mkdir(parents=True)
        (release / package / "gtfs.zip").write_bytes(b"x" * 2_500_000)
    (release / "release.json").write_text(
        json.dumps({"run_id": release.name, "completed_at": "2026-10-09T03:51:16+00:00"}),
        encoding="utf-8",
    )

    listing = write_feed_list(release, tmp_path / "public")

    assert [feed["name"] for feed in listing["feeds"]] == [
        "cz-jdf-gtfs.zip",
        "cz-czptt-gtfs.zip",
        "cz-jdf-gtfs-rt.pb",
    ]
    stored = json.loads((tmp_path / "public" / "feeds.json").read_text(encoding="utf-8"))
    assert stored["run_id"] == release.name and stored["feeds"][0]["size_bytes"] == 2_500_000
    page = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
    assert '<a href="cz-czptt-gtfs.zip">cz-czptt-gtfs.zip</a>' in page
    assert "2.5 MB" in page and release.name in page
    assert "built 9. 10. 2026 05:51" in page
    assert stored["realtime_sources"][0]["feed"] == "cz-jdf-gtfs-rt.pb"
    assert "<h2>Realtime sources</h2>" in page and "DÚK vehicle positions" in page
