from __future__ import annotations

import csv
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from obehy import filtered_jdf
from obehy.pipeline.errors import PipelineError

REFERENCE = date(2026, 10, 8)


def _jdf_file(*rows: tuple[str, ...]) -> bytes:
    text = "".join(",".join(f'"{value}"' for value in row) + ";\r\n" for row in rows)
    return text.encode("cp1250")


def _linky(
    line: str, operator: str, valid_from: str, valid_to: str, distinction: str
) -> tuple[str, ...]:
    """A JDF 1.11 Linky.txt row; only the Altdop fixture operator uses distinction 2."""

    operator_distinction = "2" if operator == "00000000" else "1"
    flags = ("A", "A", "0", "0", "0", "0", "", "", "", "")
    return (
        line,
        "Název",
        operator,
        *flags,
        valid_from,
        valid_to,
        operator_distinction,
        distinction,
    )


def _merged_jdf(
    path: Path, *, with_idzk: bool = True, linext: list[tuple[str, ...]] | None = None
) -> Path:
    """A merged JDF: PID, IDS JMK and IDZK lines, operator and alternative-operator matches."""

    linky = [
        _linky("100200", "11111111", "14122025", "12122026", "1"),  # PID, preferred
        _linky("500410", "11111111", "14122025", "12122026", "1"),  # PID only as secondary
        _linky("737001", "22222222", "14122025", "12122026", "1"),  # IDS JMK
        _linky("445017", "25220683", "01092026", "12122026", "1"),  # PMDP
        _linky("000192", "00000000", "14122025", "12122026", "2"),  # FlixBus as Altdop
        _linky("235003", "33333333", "05102026", "09102026", "3"),  # detour, no LinExt
        _linky("235003", "33333333", "02112026", "30112026", "9"),  # later regular version
        _linky("300001", "11111111", "01012026", "31012026", "1"),  # expired PID version
    ]
    if with_idzk:
        linky.append(_linky("700001", "11111111", "14122025", "12122026", "1"))
    linext = linext or [
        ("100200", "1", "30001", "200", "1", "", "1"),
        ("500410", "1", "30512", "410", "1", "", "1"),
        ("500410", "2", "30001", "410", "0", "", "1"),
        ("737001", "1", "30621", "1", "1", "", "1"),
        ("235003", "1", "30001", "MHD 3", "1", "", "9"),
        ("300001", "1", "30001", "1", "1", "", "1"),
        ("700001", "1", "30722", "1", "1", "", "1"),
    ]
    dopravci = [
        (ico, "", name, "1", "", "", "", "", "", "", "", "", distinction)
        for ico, name, distinction in (
            ("11111111", "ARRIVA STŘEDNÍ ČECHY s.r.o.", "1"),
            ("22222222", "Dopravní podnik města Brna, a.s.", "1"),
            ("25220683", "Plzeňské městské dopravní podniky, a.s.", "1"),
            ("00000000", "Centrotrans - Eurolines d.d.", "2"),
            ("00000001", "FlixBus DACH GmbH", "2"),
            ("33333333", "OAD Kolín s.r.o.", "1"),
        )
    ]
    altdop = [("000192", "0", "00000001", *[""] * 10, "2", "2")]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Linky.txt", _jdf_file(*linky))
        archive.writestr("LinExt.txt", _jdf_file(*linext))
        archive.writestr("Dopravci.txt", _jdf_file(*dopravci))
        archive.writestr("Altdop.txt", _jdf_file(*altdop))
    return path


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


def test_jdf_line_snapshot_matches_rules_from_merged_jdf(tmp_path: Path) -> None:
    merged = _merged_jdf(tmp_path / "merged-jdf.zip")

    snapshot = filtered_jdf.jdf_line_snapshot(
        merged, filtered_jdf.load_rules(), REFERENCE, tmp_path
    )

    value = json.loads(snapshot.read_text(encoding="utf-8"))
    assert value["source"] == "jdf-linext"
    assert value["reference_date"] == "2026-10-08"
    assert {
        query["value"]: query["lines"] for query in value["queries"] if query["kind"] == "ids"
    } == {
        "30001": ["100200", "235003"],
        "30621": ["737001"],
        "30722": ["700001"],
    }
    operators = next(query for query in value["queries"] if query["kind"] == "operators")
    assert operators["lines"] == ["000192", "445017"]
    assert filtered_jdf.snapshot_lines(snapshot) == {
        "000192",
        "100200",
        "235003",
        "445017",
        "700001",
        "737001",
    }


def test_rule_group_without_lines_fails(tmp_path: Path) -> None:
    merged = _merged_jdf(tmp_path / "merged-jdf.zip", with_idzk=False)

    with pytest.raises(PipelineError, match="no lines for ids=30722"):
        filtered_jdf.jdf_line_snapshot(merged, filtered_jdf.load_rules(), REFERENCE, tmp_path)


def test_unexpected_jdf_row_shape_fails(tmp_path: Path) -> None:
    merged = _merged_jdf(tmp_path / "merged-jdf.zip", linext=[("100200", "1", "30001")])

    with pytest.raises(PipelineError, match=r"LinExt\.txt row has 3 fields"):
        filtered_jdf.jdf_line_snapshot(merged, filtered_jdf.load_rules(), REFERENCE, tmp_path)


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


def test_build_filtered_jdf_publishes_feed_snapshot_and_report(tmp_path: Path) -> None:
    source = _gtfs(tmp_path / "source")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with zipfile.ZipFile(bundle / "gtfs.zip", "w") as archive:
        for path in sorted(source.iterdir()):
            archive.write(path, path.name)
    work = tmp_path / "work"
    work.mkdir()

    destination = filtered_jdf.build_filtered_jdf(
        bundle,
        tmp_path / "published",
        reference=REFERENCE,
        work=work,
        merged_jdf=_merged_jdf(tmp_path / "merged-jdf.zip"),
    )

    report = json.loads((destination / "filter-report.json").read_text(encoding="utf-8"))
    assert report["trips_kept"] == 1
    assert report["matched_lines"] == 6
    assert report["lines_removed"] == ["100200", "445017"]
    assert (destination / "line-snapshot.json").is_file()
    with zipfile.ZipFile(destination / "gtfs.zip") as archive:
        assert "stop_times.txt" in archive.namelist()
