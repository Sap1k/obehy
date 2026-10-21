"""PID alerts from Golemio (docs/sources/pid.md, Alerts): the VYMI (JIS) events as GTFS-RT
service alerts, and the VYMI infotexts departure boards show, as `Alert`s.

- `alerts`: `/v2/vehiclepositions/gtfsrt/alerts.pb`, a full GTFS-RT dataset. Informed entities
  are PID GTFS IDs (`L316` routes; stops and trips by their GTFS IDs), resolved through the
  `pid:gtfs_*` source keys. Rail lines (`L1001`…) have no key: trains get their alerts from
  SŽ. The cause and effect are GTFS-RT's; the VYMI cause is the `cause_detail` (cs, en).
- `infotexts`: `/v3/pid/infotexts?includeFuture=true`, every current and future infotext:
  stop-scoped (`related_stops`, PID GTFS stop IDs), Czech and English text, a priority (the VYMI
  severity), how a board shows it (`inline` with departures or `general` instead of them), and
  optionally a daily window (`repeat`, Europe/Prague wall time, may wrap midnight), expanded
  into periods from the day before reception for `REPEAT_DAYS`.

The full VYMI event export (`/v1/jis/events/custom-format`: type, severity, display period,
German, HTML, attachments) is internal to Golemio (403 for our key, 2026-10-21).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as rt

from obehy.realtime.alerts import Alert, AlertEntity, Period, Texts, texts
from obehy.realtime.times import Instant, ServiceTime, instant, local_date

SOURCE = "pid"
ALERTS = "alerts"
INFOTEXTS = "infotexts"
ROUTES, STOPS, TRIPS = "pid:gtfs_route_id", "pid:gtfs_stop_id", "pid:gtfs_trip_id"
REPEAT_DAYS = 7
PRIORITY = {"low": "INFO", "normal": "WARNING", "high": "SEVERE"}


class PayloadError(ValueError):
    """A successful poll whose body does not have the documented shape."""


def _texts(value: rt.TranslatedString) -> Texts:
    return tuple(sorted((t.language or "cs", t.text) for t in value.translation if t.text))


def _at(epoch: int) -> Instant:
    return instant(datetime.fromtimestamp(epoch, UTC))


def _entities(alert: rt.Alert) -> tuple[AlertEntity, ...]:
    found: list[AlertEntity] = []
    for informed in alert.informed_entity:
        if informed.HasField("trip") and informed.trip.trip_id:
            start = informed.trip.start_date
            day = date(int(start[:4]), int(start[4:6]), int(start[6:])) if start else None
            found.append(AlertEntity("trip", TRIPS, informed.trip.trip_id, day))
        elif informed.stop_id:
            found.append(AlertEntity("stop", STOPS, informed.stop_id))
        elif informed.route_id:
            found.append(AlertEntity("route", ROUTES, informed.route_id))
    return tuple(sorted(set(found), key=lambda e: (e.kind, e.identifier, e.service_date or "")))


def decode_alerts(body: bytes, received_at: Instant) -> list[Alert]:
    """A GTFS-RT alerts dataset; entities without an alert are skipped."""

    message = rt.FeedMessage()
    try:
        message.ParseFromString(body)
    except DecodeError as error:
        raise PayloadError(f"not a GTFS-RT message: {error}") from error
    alerts: list[Alert] = []
    for entity in message.entity:
        if not entity.HasField("alert") or entity.is_deleted:
            continue
        a = entity.alert
        alerts.append(
            Alert(
                source=SOURCE,
                channel=ALERTS,
                alert_id=entity.id,
                cause=rt.Alert.Cause.Name(a.cause),
                effect=rt.Alert.Effect.Name(a.effect),
                severity=rt.Alert.SeverityLevel.Name(a.severity_level),
                periods=tuple(
                    Period(
                        _at(p.start) if p.HasField("start") else None,
                        _at(p.end) if p.HasField("end") else None,
                    )
                    for p in a.active_period
                ),
                header=_texts(a.header_text),
                description=_texts(a.description_text),
                url=_texts(a.url),
                cause_detail=_texts(a.cause_detail),
                effect_detail=_texts(a.effect_detail),
                entities=_entities(a),
            )
        )
    return sorted(alerts, key=lambda alert: alert.alert_id)


def _time(value: object) -> Instant | None:
    if not isinstance(value, str) or not value:
        return None
    moment = datetime.fromisoformat(value)
    return None if moment.tzinfo is None else instant(moment)


def _seconds(clock: str) -> int:
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _periods(
    start: Instant | None, end: Instant | None, repeat: dict[str, Any], received_at: Instant
) -> tuple[Period, ...]:
    """The infotext's validity; with a daily window, each day's window inside it."""

    begin, finish = repeat.get("time_start"), repeat.get("time_end")
    if not isinstance(begin, str) or not isinstance(finish, str):
        return (Period(start, end),)
    lo, hi = _seconds(begin), _seconds(finish)
    if hi <= lo:
        hi += 24 * 3600  # wraps midnight
    first = local_date(received_at) - timedelta(days=1)
    periods: list[Period] = []
    for n in range(REPEAT_DAYS + 1):
        day = first + timedelta(days=n)
        window = Period(ServiceTime(day, lo).instant(), ServiceTime(day, hi).instant())
        assert window.start is not None and window.end is not None
        if (end is not None and window.start >= end) or (start is not None and window.end <= start):
            continue
        periods.append(
            Period(
                window.start if start is None else max(window.start, start),
                window.end if end is None else min(window.end, end),
            )
        )
    return tuple(periods)


def decode_infotexts(body: bytes, received_at: Instant) -> list[Alert]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadError(f"not JSON: {error}") from error
    if not isinstance(document, list):
        raise PayloadError("infotexts are not a list")
    alerts: list[Alert] = []
    for raw in cast(list[object], document):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        identifier, text = item.get("id"), item.get("text")
        if not isinstance(identifier, str) or not isinstance(text, str) or not text:
            continue
        stops = cast(list[dict[str, Any]], item.get("related_stops") or [])
        repeat = cast(dict[str, Any], item.get("repeat") or {})
        display = item.get("display_type")
        alerts.append(
            Alert(
                source=SOURCE,
                channel=INFOTEXTS,
                alert_id=f"infotext-{identifier}",
                cause="UNKNOWN_CAUSE",
                effect="UNKNOWN_EFFECT",
                severity=PRIORITY.get(str(item.get("priority")), "UNKNOWN_SEVERITY"),
                periods=_periods(
                    _time(item.get("valid_from")), _time(item.get("valid_to")), repeat, received_at
                ),
                header=texts(cs=text, en=cast(str | None, item.get("text_en"))),
                entities=tuple(
                    sorted(
                        {
                            AlertEntity("stop", STOPS, stop["id"])
                            for stop in stops
                            if isinstance(stop.get("id"), str)
                        },
                        key=lambda e: e.identifier,
                    )
                ),
                details=(("display_type", display),) if isinstance(display, str) else (),
            )
        )
    return sorted(alerts, key=lambda alert: alert.alert_id)
