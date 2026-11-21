"""The fleets the replay report counts bound key groups by (`replay_report.fleet`)."""

from __future__ import annotations

from obehy.realtime.replay_report import fleet


def test_duk_fleets() -> None:
    assert fleet("duk", "401234", "jdf") == "duk/teplice"
    assert fleet("duk", "301234", "jdf") == "duk/dpmul"
    assert fleet("duk", "1234", "jdf") == "duk/duk"
    assert fleet("duk", "x", "jdf") == "duk/duk"
    assert fleet("duk", "1234", "czptt") == "duk/train"


def test_mpvnet_and_pid_fleets() -> None:
    assert fleet("mpvnet", "a", "jdf") == "mpvnet/bus"
    assert fleet("mpvnet", "a", "czptt") == "mpvnet/train"
    assert fleet("pid", "trip:991_120_260207", "jdf") == "pid/metro"  # PID-Q2
    assert fleet("pid", "0:6940", "jdf") == "pid/tram"
    assert fleet("pid", "99:1", "jdf") == "pid/other"
    assert fleet("pid", "train:5861", "czptt") == "pid/train"
    assert fleet("arriva-express", "a", "jdf") == "arriva-express"
