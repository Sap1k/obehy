"""Writes the Python reference fixtures of the connectors (obehy-sources, docs/RUST_PORT.md R2).

    uv run python rust/tools/gen_sources_fixtures.py

Output goes to rust/obehy-sources/tests/fixtures/:

- cases.json: every call the Python connector unit tests make to a decoder, a recorder filter,
  the alert decoders, the board decoder and the composition decoder, captured while those tests
  run (`tests/unit/realtime/test_*_connector.py`, test_decoding, test_pid_alerts,
  test_compositions, test_boards), with Python's result or exception; plus the archived boards
  and compositions, hand-made alert payloads, recorder filter inputs and archived Arriva bodies
  re-filtered. The Rust test replays each and compares the whole result.
- json.json: `json.loads` results and error texts for awkward inputs.
- krovak.json: pyproj's EPSG:5514 -> EPSG:4326 outputs (repr, 17 significant digits) for points
  on both sides of each operation's box edge and a sample of distinct SŽ positions of a day;
  every distinct position of that day goes to data/compare-r2/krovak-day.json (gitignored) for
  the full check.

Everything is computed by the Python realtime modules themselves.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from google.transit import gtfs_realtime_pb2 as rt
from pyproj import Transformer

from obehy.realtime import times
from obehy.realtime.archive import iter_polls
from obehy.realtime.model_json import facts_to_json
from obehy.realtime.sources import (
    arriva_express,
    cd,
    decoding,
    dpmd,
    duk,
    mpvnet,
    pid,
    pid_alerts,
    sz,
    sz_tabule,
)
from obehy.realtime.times import instant

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "rust" / "obehy-sources" / "tests" / "fixtures"
LOCAL = REPO / "data" / "compare-r2"
RNG = random.Random(20261121)
CASES: list[dict[str, Any]] = []


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def us(at: datetime) -> int:
    return times.micros(instant(at))


def plain(value: object) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def observations(found: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "source": o.source,
            "channel": o.channel,
            "item": o.raw.item,
            "sha256": o.raw.sha256,
            "feed": o.feed,
            "received_at": o.received_at.isoformat(),
            "observed_at": None if o.observed_at is None else o.observed_at.isoformat(),
            "decoder_version": o.decoder_version,
            "facts": json.dumps(facts_to_json(o.facts), separators=(",", ":")),
        }
        for o in found
    ]


def record(case: dict[str, Any], call: Any, convert: Any) -> Any:
    try:
        result = call()
    except Exception as error:
        case["error"] = {"class": type(error).__name__, "message": str(error)}
        CASES.append(case)
        raise
    case["result"] = convert(result)
    CASES.append(case)
    return result


def _wrap_decoder(module: Any, source: str, channel: str | None) -> None:
    original = module.decode

    def decode(body: bytes, sha256: str, received_at: datetime, skew: timedelta, *rest: Any) -> Any:
        case = {
            "kind": "facts",
            "source": source,
            "channel": channel if channel is not None else str(rest[0]),
            "body": b64(body),
            "sha256": sha256,
            "received_at": us(received_at),
            "skew": int(skew.total_seconds() * 1_000_000),
            "request": rest[0] if module is sz_tabule else None,
        }
        return record(case, lambda: original(body, sha256, received_at, skew, *rest), observations)

    module.decode = decode


def _wrap_others() -> None:
    for module, name in ((arriva_express, "arriva-express"), (dpmd, "dpmd")):
        original = module.recorded

        def recorded(body: bytes, original: Any = original, name: str = name) -> Any:
            case = {"kind": "filter", "filter": name, "body": b64(body)}
            return record(
                case,
                lambda: original(body),
                lambda r: {"body": b64(r[0]), "kept": r[1], "dropped": r[2]},
            )

        module.recorded = recorded
    for name in ("decode_alerts", "decode_infotexts"):
        original = getattr(pid_alerts, name)

        def alerts(
            body: bytes, received_at: datetime, original: Any = original, name: str = name
        ) -> Any:
            case = {
                "kind": "alerts",
                "channel": "alerts" if name == "decode_alerts" else "infotexts",
                "body": b64(body),
                "received_at": us(received_at),
            }
            return record(case, lambda: original(body, received_at), plain)

        setattr(pid_alerts, name, alerts)
    original_cd = cd.decode

    def composition(body: bytes, request: Any) -> Any:
        case = {"kind": "composition", "body": b64(body)}
        return record(case, lambda: original_cd(body, request), plain)

    cd.decode = composition
    original_json = decoding.load_json

    def load_json(body: bytes | str) -> Any:
        if isinstance(body, str):
            return original_json(body)
        case = {"kind": "json", "body": b64(body)}
        return record(
            case, lambda: original_json(body), lambda r: json.dumps(r, separators=(",", ":"))
        )

    decoding.load_json = load_json


class Capture:
    """Wraps the connectors while the Python unit tests run."""

    def pytest_sessionstart(self, session: Any) -> None:
        del session
        _wrap_decoder(duk, "duk", "vehicles")
        _wrap_decoder(pid, "pid", "vehicles")
        _wrap_decoder(sz, "sz-mapa", "trains")
        _wrap_decoder(dpmd, "dpmd", "vehicles")
        _wrap_decoder(arriva_express, "arriva-express", "buses")
        _wrap_decoder(mpvnet, "mpvnet", None)
        _wrap_decoder(sz_tabule, "sz-tabule", "board")
        _wrap_others()


def unit_test_cases() -> None:
    tests = REPO / "tests" / "unit" / "realtime"
    names = [
        "test_duk_connector.py",
        "test_pid_connector.py",
        "test_sz_connector.py",
        "test_dpmd_connector.py",
        "test_arriva_express_connector.py",
        "test_mpvnet_connector.py",
        "test_decoding.py",
        "test_pid_alerts.py",
        "test_compositions.py",
        "test_boards.py",
    ]
    code = pytest.main(
        ["-q", "-p", "no:cacheprovider", *[str(tests / n) for n in names]], plugins=[Capture()]
    )
    if code != 0:
        sys.exit(f"the Python connector tests failed ({code})")


def extra_cases() -> None:
    received = instant(datetime(2026, 10, 10, 11, 36, tzinfo=UTC))
    boards = REPO / "tests" / "realtime" / "boards"
    for path in sorted(boards.glob("*.html")):
        station = path.stem.rsplit("-", 1)[1]
        requests = [{"sr70": station}] + (
            [{"sr70": int(station)}, {}] if "ponikla" in path.stem else []
        )
        for request in requests:
            with contextlib.suppress(Exception):
                sz_tabule.decode(
                    path.read_bytes(), "b" * 64, received, timedelta(minutes=20), request
                )
    compositions = REPO / "tests" / "realtime" / "compositions"
    for path in sorted(compositions.glob("*.json")):
        with contextlib.suppress(Exception):
            cd.decode(path.read_bytes(), None)  # type: ignore[arg-type]
    for body in (b'\xef\xbb\xbf  ({"d":{"Trains":[]}});', b'{"d":[]}', b"(\xff)", b"[]"):
        with contextlib.suppress(Exception):
            cd.decode(body, None)  # type: ignore[arg-type]
    for body in alert_payloads():
        with contextlib.suppress(Exception):
            pid_alerts.decode_alerts(body, received)
    for body in infotext_payloads():
        with contextlib.suppress(Exception):
            pid_alerts.decode_infotexts(body, received)
    for body in dpmd_scripts():
        with contextlib.suppress(Exception):
            dpmd.recorded(body)
    for body in arriva_bodies():
        with contextlib.suppress(Exception):
            arriva_express.recorded(body)


def alert_payloads() -> list[bytes]:
    payloads: list[bytes] = []
    message = rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    for n in range(4):
        entity = message.entity.add()
        entity.id = f"alert-{3 - n}"
        alert = entity.alert
        alert.cause = [1, 3, 13, 6][n]
        alert.effect = [8, 1, 11, 3][n]
        if n:
            alert.severity_level = n + 1
        period = alert.active_period.add()
        period.start = 1_760_000_000 + n
        if n % 2:
            period.end = 1_760_100_000
        alert.active_period.add()
        for lang, text in (("cs", f"Výluka {n}"), ("en", f"Closure {n}"), ("", "bez jazyka")):
            t = alert.header_text.translation.add()
            t.text = text
            if lang:
                t.language = lang
        alert.description_text.translation.add().text = "popis"
        alert.cause_detail.translation.add().text = ""
        alert.url.translation.add(text="https://pid.cz", language="cs")
        for route in ("L316", "L991", "L316"):
            alert.informed_entity.add().route_id = route
        alert.informed_entity.add().stop_id = "U1072Z1P"
        trip = alert.informed_entity.add().trip
        trip.trip_id = f"316_{n}_261020"
        if n % 2:
            trip.start_date = "20261020"
        alert.informed_entity.add()
    deleted = message.entity.add()
    deleted.id = "gone"
    deleted.is_deleted = True
    deleted.alert.cause = 2
    message.entity.add().id = "no-alert"
    payloads.append(message.SerializeToString())
    payloads.append(b"\xff\xff\xff")
    payloads.append(b"")
    return payloads


def infotext_payloads() -> list[bytes]:
    items = [
        {
            "id": "a",
            "text": "Výluka",
            "text_en": "Closure",
            "priority": "high",
            "display_type": "inline",
            "valid_from": "2026-10-09T10:00:00+02:00",
            "valid_to": "2026-10-14T22:00:00+02:00",
            "related_stops": [{"id": "U2"}, {"id": "U1"}, {"id": "U2"}, {"x": 1}],
            "repeat": {"time_start": "22:00:00", "time_end": "04:30:00"},
        },
        {
            "id": "b",
            "text": "Bez platnosti",
            "priority": "normal",
            "repeat": {"time_start": "06:00:00", "time_end": "08:00:00"},
        },
        {"id": "c", "text": "", "priority": "low"},
        {"id": 5, "text": "x"},
        {"id": "d", "text": "naive", "valid_from": "2026-10-09T10:00:00", "priority": None},
        {
            "id": "e",
            "text": "low",
            "priority": "low",
            "valid_to": "2026-10-10T09:00:00Z",
            "display_type": 3,
        },
        "not an object",
    ]
    return [
        json.dumps(items).encode(),
        json.dumps({"x": 1}).encode(),
        json.dumps([{"id": "f", "text": "t", "valid_from": "nope"}]).encode(),
        json.dumps(
            [{"id": "g", "text": "t", "repeat": {"time_start": "22:00", "time_end": "04:00:00"}}]
        ).encode(),
    ]


def dpmd_scripts() -> list[bytes]:
    head = '/* {"TimeStamp": "2026-10-12T07:00:01.234"} */\n'
    body = (
        "datahash = 'x';\n"
        "buspositions = [{id: 1, line: `201`, vehicle: '830', off: 0, driver: 7, "
        "body: `<b>830</b><br>Linka: 201<br>Řidič: 12345<br>Spoj: 12<br>Čas: 12.10.2026 08:59:35`"
        " + imageUrl + `x.jpg`, arrow: '135', coords: \"50.75N, 14.18E\", fuel: 1.50, hue: -3, "
        "final1: null, extra: true}, {id: 2, vehicle: '1', off: 1},"
        " {id: 3, vehicle: '2', off: 1.0},"
        " {id: 4, vehicle: 'é\\n\\'q\\'', course: [1, 2.25, -0, 007]}];\n"
        "stops = [{name: 'Hl.nádr.'}];\n"
    )
    return [
        (head + body).encode(),
        (b'/* {"TimeStamp": 5} */ x = 1;'),
        (b"x = 1;"),
        (head + "buspositions = {};").encode(),
        (head + "buspositions = [1];").encode(),
        (head + "buspositions = [`unterminated").encode(),
        (head + "buspositions = [`a${b}`];").encode(),
        (head + "buspositions = [`a\\q`];").encode(),
        (head + "buspositions = [nope];").encode(),
        (head + "buspositions = []").encode(),
        (head + "buspositions = [];").encode() + b"\xff",
        (b'/* {"TimeStamp": "2026-10-12T07:00:01"} */ buspositions = [{a: `x`}]; TimeStamp = 3;'),
    ]


def arriva_bodies() -> list[bytes]:
    bodies = [
        json.dumps(
            [
                {
                    "data": {
                        "busesCurrentLocations": [
                            {
                                "mainType": "ARRIVA EXPRESS",
                                "spz": " 1AB 2345 ",
                                "angle": 90.0,
                                "latitude": 49.123456789012,
                                "delay": -1,
                                "x": [1e-7, 1e16, 2.5, 123456789012345678901234567890],
                            },
                            {"mainType": "ARRIVA", "spz": "X"},
                            1,
                            None,
                        ]
                    },
                    "extensions": {"č": "ž"},
                }
            ],
            ensure_ascii=False,
        ).encode(),
        b"[]",
        b"{}",
        b"[1]",
        b'[{"data": {"busesCurrentLocations": {}}}]',
        b"<html>",
    ]
    root = REPO.parent / "rt-data" / "rt-raw"
    for poll in list(
        iter_polls(root, "arriva-express", "buses", date(2026, 10, 6), date(2026, 10, 6))
    )[:40:8]:
        body = poll.body()
        if body is not None:
            bodies.append(body)
    return bodies


def json_cases() -> list[dict[str, Any]]:
    inputs = [
        b"",
        b" ",
        b"<html>",
        b"{",
        b"[",
        b"[1,]",
        b'{"a":1,}',
        b'{"a" 1}',
        b'{"a":}',
        b"{1:2}",
        b"[1 2]",
        b"[1]x",
        b"nul",
        b"-",
        b"-x",
        b"1.",
        b"1e",
        b"1e+",
        b"01",
        b"-0",
        b"-0.0",
        b'"\\x"',
        b'"\\u12"',
        b'"\\u12g4"',
        b'"abc',
        b'"a\\',
        b'"a\x01"',
        b'"\\ud83d\\ude00"',
        b'"\\ud83d\\u0041"',
        b'"\\ud83dx"',
        b"\n\n  [1,\n  ?]",
        '["á", ?]'.encode(),
        b"NaN",
        b"-Infinity",
        b"Infinity",
        b"1e400",
        b"-1e400",
        b"123456789012345678901234567890",
        b'{"a":1,"b":2,"a":3}',
        b"\xef\xbb\xbf[1]",
        b"\xff",
        b"a\xc3",
        b"\xe2\x82\x41",
        b"\xe0\x80",
        b"\xf5",
        '"€"'.encode("utf-16"),
        "[1]".encode("utf-16-le"),
        "[1]".encode("utf-16-be"),
        "[1]".encode("utf-32"),
        "[1]".encode("utf-32-le"),
        b"  1  ",
        b"[[[]]]",
        b'{"x": [1.0, 2.50, 1E5, -0.0, 0.1]}',
        b"9" * 4301,
    ]
    out = []
    for body in inputs:
        try:
            result = decoding.load_json(body)
            out.append({"body": b64(body), "ok": json.dumps(result, separators=(",", ":"))})
        except Exception as error:
            out.append(
                {"body": b64(body), "error": {"class": type(error).__name__, "message": str(error)}}
            )
    return out


SLOVAKIA = ((-598456.5382383128, -164288.7463043341), (-1353322.8862039084, -1116718.8694003904))
CZECHIA = ((-935779.7018432004, -418730.0012848261), (-1244199.8543909981, -911137.7509007935))


def edge_points() -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for (x0, x1), (y0, y1) in (SLOVAKIA, CZECHIA):
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
        for d in (-1.0, -1e-6, 0.0, 1e-6, 1.0):
            points += [(x0 + d, ym), (x1 + d, ym), (xm, y0 + d), (xm, y1 + d)]
            points += [(x0 + d, y0 + d), (x1 + d, y1 + d)]
    points += [
        (-742000.0, -1043000.0),
        (-500000.0, -1200000.0),
        (-300000.0, -1250000.0),
        (-1000000.0, -1300000.0),
        (-100000.0, -900000.0),
        (-450000.0, -1150000.0),
    ]
    return points


def day_points() -> list[tuple[float, float]]:
    root = REPO / "data" / "rt-raw-prod"
    found: set[tuple[float, float]] = set()
    for poll in iter_polls(root, "sz-mapa", "trains", date(2026, 10, 12), date(2026, 10, 12)):
        body = poll.body()
        if body is None:
            continue
        try:
            document = json.loads(body)
        except ValueError:
            continue
        for feature in document.get("result", []) if isinstance(document, dict) else []:
            coords = (
                (feature.get("geometry") or {}).get("coordinates")
                if isinstance(feature, dict)
                else None
            )
            if (
                isinstance(coords, list)
                and len(coords) == 2
                and all(
                    isinstance(c, int | float) and not isinstance(c, bool) and c != 0
                    for c in coords
                )
            ):
                found.add((float(coords[0]), float(coords[1])))
    return sorted(found)


def krovak() -> None:
    transformer = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)

    def rows(points: list[tuple[float, float]]) -> list[list[str]]:
        out = []
        for x, y in points:
            lon, lat = transformer.transform(x, y)
            out.append([repr(x), repr(y), repr(lon), repr(lat)])
        return out

    day = day_points()
    sample = RNG.sample(day, min(3000, len(day)))
    (OUT / "krovak.json").write_text(
        json.dumps(rows(edge_points() + sorted(sample))) + "\n", "utf-8"
    )
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / "krovak-day.json").write_text(json.dumps(rows(day)) + "\n", "utf-8")
    print(f"krovak: {len(day)} distinct positions of 2026-10-12")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    unit_test_cases()
    extra_cases()
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in CASES:
        key = json.dumps(case, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(case)
    (OUT / "cases.json").write_text(
        json.dumps(unique, ensure_ascii=False, indent=0) + "\n", "utf-8"
    )
    (OUT / "json.json").write_text(
        json.dumps(json_cases(), ensure_ascii=False, indent=0) + "\n", "utf-8"
    )
    krovak()
    print(f"{len(unique)} cases")


if __name__ == "__main__":
    main()
