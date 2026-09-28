from __future__ import annotations

import csv
import json
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from obehy import filtered_jdf
from obehy.national_jdf import PipelineError

PORTAL_HTML = """
<html><body>
<table id="filtr"><tr><td>999999</td><td>x</td><td>not results</td></tr></table>
<table id="vysledky">
  <tr><th>Linka</th><th>IDS</th><th>Název</th><th>Dopravce</th></tr>
  <tr><td>445017</td><td></td><td>Doubravka - Nová Hospoda</td><td>PMDP</td></tr>
  <tr><td>100200</td><td>PID 200</td><td>Praha - Kladno</td><td>ARRIVA</td></tr>
  <tr><td>n/a</td><td></td><td>ignored</td></tr>
</table>
</body></html>
"""


def _write(path: Path, header: str, *rows: str) -> None:
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")


def _gtfs(root: Path) -> Path:
    root.mkdir()
    _write(root / "agency.txt", "agency_id,agency_name", "a1,One", "a2,Two")
    _write(
        root / "routes.txt",
        "route_id,agency_id,route_short_name,route_long_name,route_type",
        "jdf:route:100200:1,a1,200,Praha - Kladno,3",
        "jdf:route:445017:1,a2,17,Plzeň,3",
        "jdf:route:300001:1,a1,1,Kept,3",
        "jdf:route:300002:1,a2,2,Too short,3",
    )
    _write(
        root / "trips.txt",
        "route_id,service_id,trip_id",
        "jdf:route:100200:1,s1,t-removed",
        "jdf:route:445017:1,s2,t-prefix",
        "jdf:route:300001:1,s1,t-kept",
        "jdf:route:300002:1,s3,t-short",
    )
    _write(
        root / "stops.txt",
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station",
        "p1,Město,50,14,1,",
        "p1:u,Město,50,14,0,p1",
        'p2,"Leszna Górna,CLO",49.7,18.7,1,',
        'p2:u,"Leszna Górna,CLO",49.7,18.7,0,p2',
        "p3,Nikde,0,0,1,",
        "p3:u,Nikde,0,0,0,p3",
        "p4,Konec,50.1,14.1,1,",
        "p4:u,Konec,50.1,14.1,0,p4",
        "p5,Seifhennersdorf Zollstr.,50.9,14.6,1,",
        "p5:u,Seifhennersdorf Zollstr.,50.9,14.6,0,p5",
    )
    _write(
        root / "stop_times.txt",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type",
        "t-removed,08:00:00,08:00:00,p1:u,1,0,0",
        "t-removed,08:10:00,08:10:00,p4:u,2,0,0",
        "t-prefix,08:00:00,08:00:00,p1:u,1,0,0",
        "t-prefix,08:10:00,08:10:00,p4:u,2,0,0",
        "t-kept,09:00:00,09:00:00,p1:u,1,0,0",
        "t-kept,09:05:00,09:05:00,p3:u,2,0,0",
        "t-kept,09:10:00,09:10:00,p2:u,3,0,0",
        "t-kept,09:15:00,09:15:00,p5:u,4,0,0",
        "t-short,10:00:00,10:00:00,p3:u,1,0,0",
        "t-short,10:10:00,10:10:00,p4:u,2,0,0",
    )
    _write(
        root / "calendar.txt",
        "service_id,monday,start_date,end_date",
        "s1,1,20260101,20261231",
        "s2,1,20260101,20261231",
        "s3,1,20260101,20261231",
    )
    _write(root / "feed_info.txt", "feed_publisher_name,feed_lang", "Oběhy,cs")
    return root


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_parse_portal_results_reads_only_result_table() -> None:
    lines = filtered_jdf.parse_portal_results(PORTAL_HTML)

    assert [line.line for line in lines] == ["445017", "100200"]
    assert lines[1].ids == "PID 200"
    assert lines[0].name == "Doubravka - Nová Hospoda"


def test_filter_cascades_and_removes_calls_without_coordinates(tmp_path: Path) -> None:
    source = _gtfs(tmp_path / "source")
    output = tmp_path / "output"

    report = filtered_jdf.filter_gtfs(
        source, output, removed_lines={"100200"}, line_prefixes=("445",)
    )

    assert [row["trip_id"] for row in _read(output / "trips.txt")] == ["t-kept"]
    assert [row["route_id"] for row in _read(output / "routes.txt")] == ["jdf:route:300001:1"]
    assert [row["agency_id"] for row in _read(output / "agency.txt")] == ["a1"]
    assert [row["service_id"] for row in _read(output / "calendar.txt")] == ["s1"]
    calls = _read(output / "stop_times.txt")
    assert [row["stop_id"] for row in calls] == ["p1:u", "p2:u", "p5:u"]
    assert {(row["pickup_type"], row["drop_off_type"]) for row in calls} == {("0", "0")}
    assert {row["stop_id"] for row in _read(output / "stops.txt")} == {
        "p1",
        "p1:u",
        "p2",
        "p2:u",
        "p5",
        "p5:u",
    }
    assert (output / "feed_info.txt").read_text(encoding="utf-8").startswith("feed_publisher")
    assert report["routes_removed"] == 2
    assert report["lines_removed"] == ["100200", "445017"]
    assert report["calls_removed_missing_coordinates"] == 1
    assert report["trips_removed_too_few_calls"] == 1


def test_build_filtered_jdf_queries_portal_and_publishes(tmp_path: Path) -> None:
    source = _gtfs(tmp_path / "source")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with zipfile.ZipFile(bundle / "gtfs.zip", "w") as archive:
        for path in sorted(source.iterdir()):
            archive.write(path, path.name)
    requests: list[dict[str, list[str]]] = []

    def post(url: str, body: bytes) -> bytes:
        assert url == filtered_jdf.PORTAL_URL
        requests.append(parse_qs(body.decode("ascii"), keep_blank_values=True))
        return PORTAL_HTML.encode("utf-8")

    work = tmp_path / "work"
    work.mkdir()
    destination = filtered_jdf.build_filtered_jdf(
        bundle, tmp_path / "published", reference=date(2026, 9, 28), work=work, post=post
    )

    rules = filtered_jdf.load_rules()
    assert len(requests) == 1 + len(rules.ids_codes)
    assert requests[0]["dopravci_ano[]"] == list(rules.operators)
    assert requests[1]["koddopravy"] == ["30001"]
    assert requests[1]["datum_od"] == ["2026-09-28"]
    report = json.loads((destination / "filter-report.json").read_text(encoding="utf-8"))
    assert report["trips_kept"] == 1
    assert report["portal_lines"] == 2
    with zipfile.ZipFile(destination / "gtfs.zip") as archive:
        assert "stop_times.txt" in archive.namelist()
    replayed = filtered_jdf.build_filtered_jdf(
        bundle,
        tmp_path / "replayed",
        reference=date(2026, 9, 28),
        work=work,
        line_snapshot=destination / "line-snapshot.json",
        post=lambda _url, _body: pytest.fail("snapshot replay must not query the portal"),
    )
    assert (replayed / "gtfs.zip").read_bytes() == (destination / "gtfs.zip").read_bytes()


def test_empty_portal_answer_fails(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="no lines"):
        filtered_jdf.fetch_line_snapshot(
            filtered_jdf.load_rules(),
            date(2026, 9, 28),
            tmp_path,
            post=lambda _url, _body: b"<table id='vysledky'></table>",
        )
