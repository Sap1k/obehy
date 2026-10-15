from __future__ import annotations

import json
from pathlib import Path

from obehy.realtime.record import Channel, Source, load_sources
from obehy.release.feed_list import feed_list, write_feed_list


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
        "cz-czptt-gtfs-rt.pb",
    ]
    stored = json.loads((tmp_path / "public" / "feeds.json").read_text(encoding="utf-8"))
    assert stored["run_id"] == release.name and stored["feeds"][0]["size_bytes"] == 2_500_000
    page = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
    assert '<a href="cz-czptt-gtfs.zip">cz-czptt-gtfs.zip</a>' in page
    assert "2.5 MB" in page and release.name in page
    assert "built 9. 10. 2026 05:51" in page
    assert "<h2>Realtime sources</h2>" in page and "DÚK vehicle positions" in page


def test_realtime_sources_come_from_the_connector_manifests(tmp_path: Path) -> None:
    release = tmp_path / "20261013T030215Z-8cf199243726"
    release.mkdir()
    (release / "release.json").write_text(json.dumps({"run_id": release.name}), encoding="utf-8")

    feeds = {row["source"]: row["feeds"] for row in feed_list(release)["realtime_sources"]}

    assert feeds["duk"] == ["cz-jdf-gtfs-rt.pb", "cz-czptt-gtfs-rt.pb"]
    assert feeds["sz-mapa"] == ["cz-czptt-gtfs-rt.pb"]
    # MPV net trains are recorded but not live (live_feeds = ["jdf"]).
    assert feeds["mpvnet"] == ["cz-jdf-gtfs-rt.pb"]
    assert feeds["pid"] == ["cz-jdf-gtfs-rt.pb", "cz-czptt-gtfs-rt.pb"]
    assert set(feeds) == {source.source for source in load_sources() if source.live_feeds}


def test_a_source_live_on_no_feed_is_not_listed(tmp_path: Path) -> None:
    release = tmp_path / "20261013T030215Z-8cf199243726"
    release.mkdir()
    (release / "release.json").write_text(json.dumps({"run_id": release.name}), encoding="utf-8")
    recorded = Source(
        "probe",
        "Probe",
        "Recorded only.",
        (
            Channel(
                "probe", "c", "GET", "https://example.test", 10, 5, feeds=("jdf",), live_feeds=()
            ),
        ),
    )

    assert feed_list(release, [recorded])["realtime_sources"] == []
