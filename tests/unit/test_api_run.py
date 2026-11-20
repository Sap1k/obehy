"""The alerts a rail run shows (`trips.run_alerts`): its parts' own and their lines', and a
stop's only at a stop still ahead."""

from __future__ import annotations

from datetime import datetime

from obehy.api import schema
from obehy.api.trips import RunPart, run_alerts

NOW = datetime.fromisoformat("2026-10-12T08:00:00+02:00")


def _line(route_id: str) -> schema.Line:
    return schema.Line(
        route_id=route_id, short_name=route_id[-1], long_name=None, mode="rail", color=None,
        text_color=None,
    )  # fmt: skip


def _alert(alert_id: str, *informs: dict[str, str]) -> schema.Alert:
    return schema.Alert(
        alert_id=alert_id, source="pid", source_label=None, cause="UNKNOWN_CAUSE",
        effect="OTHER_EFFECT", severity="SEVERE", periods=[], header={}, description={}, url={},
        cause_detail={}, effect_detail={}, details={},
        informs=[schema.AlertTarget.model_validate({"kind": "stop", "feed": "jdf"} | t)
                 for t in informs],
        first_seen=NOW, changed_at=NOW, withdrawn_at=None,
    )  # fmt: skip


def _call(trip_id: str, location_id: str, name: str, passed: bool) -> schema.RunCall:
    call = schema.TripCall(
        sequence=1, location_id=location_id, name=name, latitude=None, longitude=None,
        boarding="regular", alighting="regular", flags=[], platform=None,
        scheduled_arrival=None, scheduled_departure=None, estimated_arrival=None,
        estimated_departure=None, status="scheduled", source_class=None, delay_min=None,
        punctuality="unknown", passed=passed,
    )  # fmt: skip
    return schema.RunCall(trip_id=trip_id, continues_as=None, call=call)


CALLS = [
    _call("jdf:trip:1", "jdf:stop:1", "Florenc", passed=True),
    _call("jdf:trip:1", "jdf:stop:3", "Florenc", passed=False),
    _call("jdf:trip:2", "jdf:stop:4", "Hlavní nádraží", passed=False),
]


def _parts(*alerts: schema.Alert) -> list[RunPart]:
    return [
        RunPart("jdf:trip:1", "jdf:route:1", _line("jdf:route:1"), None, list(alerts)),
        RunPart("jdf:trip:2", "jdf:route:2", _line("jdf:route:2"), "Os 2", []),
    ]


def test_the_runs_line_and_trip_alerts_show_whatever_their_stops() -> None:
    line = _alert("line", {"kind": "route", "id": "jdf:route:1"})
    next_part = _alert("next-part", {"kind": "trip", "id": "jdf:trip:2"})
    shown = run_alerts(CALLS, 1, _parts(line, next_part))
    assert [(a.alert.alert_id, a.call, a.line.route_id, a.trip_short_name) for a in shown] == [
        ("line", None, "jdf:route:1", None),
        ("next-part", None, "jdf:route:2", "Os 2"),
    ]


def test_a_stops_alert_shows_only_at_a_stop_still_ahead() -> None:
    ahead = _alert("ahead", {"id": "jdf:stop:4"})
    passed = _alert("passed", {"id": "jdf:stop:1"})
    post = _alert("post-ahead", {"id": "jdf:stop:3:post:2", "stop_place": "jdf:stop:3"})
    shown = run_alerts(CALLS, 1, _parts(ahead, passed, post))
    assert [(a.alert.alert_id, a.call) for a in shown] == [("ahead", 2), ("post-ahead", 1)]


def test_another_lines_alert_at_no_stop_ahead_is_left_out() -> None:
    other = _alert("other", {"kind": "route", "id": "jdf:route:9"})
    assert run_alerts(CALLS, 1, _parts(other)) == []
    # Everything passed: no stop is ahead.
    assert run_alerts(CALLS, None, _parts(_alert("ahead", {"id": "jdf:stop:4"}))) == []
