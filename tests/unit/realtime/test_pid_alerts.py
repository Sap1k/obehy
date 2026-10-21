"""PID alerts and infotexts from Golemio (docs/sources/pid.md, Alerts), and their GTFS-RT form."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from google.transit import gtfs_realtime_pb2 as rt

from obehy.realtime.alerts import AlertEntity, Period
from obehy.realtime.emit.alerts import alert_entity
from obehy.realtime.sources import pid_alerts
from obehy.realtime.times import instant
from tests.realtime.observations import local

RECEIVED = instant(datetime(2026, 10, 21, 16, 27, tzinfo=UTC))


def _message() -> bytes:
    """Shaped like the 2026-10-21 probe: route-scoped VYMI events, Czech texts."""

    message = rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    entity = message.entity.add()
    entity.id = "alert-6de45eb6"
    alert = entity.alert
    alert.active_period.add().start = 1792593900
    for route in ("L1240", "L1040", "L1040"):
        alert.informed_entity.add().route_id = route
    alert.cause = rt.Alert.ACCIDENT
    alert.effect = rt.Alert.NO_SERVICE
    alert.url.translation.add(text="https://pid.cz/mimoradnost/?uuid=6de45eb6", language="cs")
    alert.header_text.translation.add(text="Chlumčany u Loun - Vrbno nad Lesy", language="cs")
    alert.description_text.translation.add(text="Vlak Os 9715 NEJEDE.", language="cs")
    alert.cause_detail.translation.add(text="Překážka na trati/cestě", language="cs")
    alert.cause_detail.translation.add(text="Obstacle on road/tracks", language="en")
    message.entity.add().id = "not-an-alert"
    return message.SerializeToString()


def test_pid_q9_alerts_are_route_scoped_and_name_rail_lines_too() -> None:
    (alert,) = pid_alerts.decode_alerts(_message(), RECEIVED)
    assert (alert.source, alert.channel, alert.alert_id) == ("pid", "alerts", "alert-6de45eb6")
    assert (alert.cause, alert.effect, alert.severity) == (
        "ACCIDENT",
        "NO_SERVICE",
        "UNKNOWN_SEVERITY",
    )
    assert alert.periods == (Period(instant(datetime.fromtimestamp(1792593900, UTC)), None),)
    # Named as PID does, deduplicated; rail lines too (they resolve to nothing: SŽ covers trains).
    assert alert.entities == (
        AlertEntity("route", "pid:gtfs_route_id", "L1040"),
        AlertEntity("route", "pid:gtfs_route_id", "L1240"),
    )
    assert alert.cause_detail == (
        ("cs", "Překážka na trati/cestě"),
        ("en", "Obstacle on road/tracks"),
    )


def _infotext(**changes: object) -> bytes:
    item: dict[str, object] = {
        "priority": "high",
        "display_type": "inline",
        "text": "Přerušen provoz TRAM v obratišti Zvonařka.",
        "text_en": "No TRAM service at Zvonařka turning loop.",
        "related_stops": [
            {"id": "U515Z1P", "name": "Olšanské hřbitovy", "platform_code": "A"},
            {"id": "U209Z1P", "name": "Jiřího z Poděbrad", "platform_code": "A"},
        ],
        "valid_from": "2026-10-17T20:55:00+02:00",
        "valid_to": None,
        "repeat": {"time_start": None, "time_end": None},
        "id": "c59e1287",
    }
    return json.dumps([item | changes]).encode()


def test_pid_infotexts_are_stop_scoped_with_both_languages() -> None:
    (alert,) = pid_alerts.decode_infotexts(_infotext(), RECEIVED)
    assert (alert.channel, alert.alert_id, alert.severity) == (
        "infotexts",
        "infotext-c59e1287",
        "SEVERE",
    )
    assert dict(alert.header) == {
        "cs": "Přerušen provoz TRAM v obratišti Zvonařka.",
        "en": "No TRAM service at Zvonařka turning loop.",
    }
    assert [e.identifier for e in alert.entities] == ["U209Z1P", "U515Z1P"]
    assert alert.periods == (Period(local("2026-10-17 20:55"), None),)
    assert alert.details == (("display_type", "inline"),)


def test_pid_q10_a_daily_infotext_window_wraps_midnight_within_its_validity() -> None:
    (alert,) = pid_alerts.decode_infotexts(
        _infotext(
            valid_from="2026-10-21T00:00:00+02:00",
            valid_to="2026-10-23T12:00:00+02:00",
            repeat={"time_start": "23:00:00", "time_end": "04:30:00"},
        ),
        RECEIVED,
    )
    assert alert.periods == (
        Period(local("2026-10-21 00:00"), local("2026-10-21 04:30")),  # the 20th's window
        Period(local("2026-10-21 23:00"), local("2026-10-22 04:30")),
        Period(local("2026-10-22 23:00"), local("2026-10-23 04:30")),
    )


def test_an_alert_is_republished_under_our_ids_in_each_feed() -> None:
    row = {
        "source": "pid",
        "alert_id": "alert-6de45eb6",
        "cause": "ACCIDENT",
        "effect": "NO_SERVICE",
        "severity": "UNKNOWN_SEVERITY",
        "periods": [{"start": "2026-10-21T13:25:00+00:00", "end": None}],
        "header": {"cs": "Chlumčany u Loun - Vrbno nad Lesy"},
        "description": {},
        "url": {"cs": "https://pid.cz/mimoradnost/?uuid=6de45eb6"},
        "cause_detail": {"cs": "Překážka", "en": "Obstacle"},
        "effect_detail": {},
        "targets": [
            {"kind": "route", "id": "jdf:route:100316", "service_date": None},
            {"kind": "stop", "id": "overlay:pid-gtfs:post:1", "service_date": None},
            {"kind": "trip", "id": "jdf:trip:1", "service_date": "2026-10-21"},
        ],
    }
    entity = rt.FeedEntity.FromString(alert_entity(row))
    assert entity.id == "alert:pid:alert-6de45eb6"
    alert = entity.alert
    assert [
        (e.route_id, e.stop_id, e.trip.trip_id, e.trip.start_date) for e in alert.informed_entity
    ] == [
        ("jdf:route:100316", "", "", ""),
        ("", "overlay:pid-gtfs:post:1", "", ""),
        ("", "", "jdf:trip:1", "20261021"),
    ]
    assert (alert.cause, alert.effect) == (rt.Alert.ACCIDENT, rt.Alert.NO_SERVICE)
    assert alert.active_period[0].start == 1792589100 and not alert.active_period[0].HasField("end")
    assert [(t.language, t.text) for t in alert.cause_detail.translation] == [
        ("cs", "Překážka"),
        ("en", "Obstacle"),
    ]
