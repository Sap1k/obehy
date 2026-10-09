from __future__ import annotations

import zipfile
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as rt

from obehy import cli


def _gtfs(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("trips.txt", "route_id,service_id,trip_id\nr,s,t1\n")
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,08:00:00,08:00:00,a,1\nt1,08:05:00,08:05:00,b,2\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
            "start_date,end_date\ns,1,1,1,1,1,0,0,20261001,20261031\n",
        )
    return path


def _snapshot(path: Path, times: list[int], trip_id: str = "t1") -> Path:
    message = rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    entity = message.entity.add()
    entity.id = "x"
    entity.trip_update.trip.trip_id = trip_id
    entity.trip_update.trip.start_date = "20261008"
    for sequence, value in enumerate(times, start=1):
        stop = entity.trip_update.stop_time_update.add()
        stop.stop_sequence = sequence
        stop.arrival.time = value
    path.write_bytes(message.SerializeToString())
    return path


def test_consistent_snapshots_pass_and_broken_ones_fail(tmp_path: Path) -> None:
    gtfs = _gtfs(tmp_path / "gtfs.zip")
    good = _snapshot(tmp_path / "good.pb", [100, 200])
    assert cli.main(["rt", "check-gtfs-rt", "--gtfs", str(gtfs), str(good)]) == 0
    backwards = _snapshot(tmp_path / "bad.pb", [200, 100])
    assert cli.main(["rt", "check-gtfs-rt", "--gtfs", str(gtfs), str(backwards)]) == 1
    unknown = _snapshot(tmp_path / "unknown.pb", [100], trip_id="t9")
    assert cli.main(["rt", "check-gtfs-rt", "--gtfs", str(gtfs), str(unknown)]) == 1
