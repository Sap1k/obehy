"""Writes the Python reference fixtures the Rust tests compare against (docs/RUST_PORT.md, R1).

    uv run python rust/tools/gen_fixtures.py

Everything is computed by the Python realtime modules themselves (times, geo, policy,
manifest, model_json and the connectors), never re-implemented here. Output goes to
rust/obehy-core/tests/fixtures/; regenerate when the Python side changes (and on a tzdata
bump, after copying the new Europe/Prague TZif, see obehy-core/src/time/zone.rs).

Facts come from archived payloads where an archive is present (the first matching day of each
channel under the roots below, a few polls each) plus hand-made facts covering every fact type,
every optional field and awkward floats and strings.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
import struct
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import tzdata

from obehy.realtime import geo, times
from obehy.realtime.archive import iter_polls
from obehy.realtime.facts import (
    Delay,
    Destination,
    Fact,
    Interval,
    LineRef,
    NextPoint,
    NextStopName,
    NextStopPrediction,
    Observation,
    PlatformAssignment,
    PointEvent,
    Position,
    RawRef,
    ServiceDay,
    SourceDeparture,
    SourceState,
    TripCancelled,
    TripKey,
    TripStatus,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.manifest import SOURCES, load_sources
from obehy.realtime.model_json import facts_to_json
from obehy.realtime.policy import POLICY, load_policy
from obehy.realtime.record import FILTERS
from obehy.realtime.sources.registry import DECODERS
from obehy.realtime.times import Instant, ServiceTime, instant

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "rust" / "obehy-core" / "tests" / "fixtures"
ARCHIVES = [
    REPO / "data" / "rt-raw-prod",
    REPO.parent / "rt-data" / "rt-raw",
    REPO / "data" / "rt-raw",
]
POLLS_PER_CHANNEL = 4
OBSERVATIONS_PER_CHANNEL = 120
RNG = random.Random(20261121)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bits(x: float) -> str:
    return f"{struct.unpack('<Q', struct.pack('<d', x))[0]:016x}"


def us(at: datetime) -> int:
    return times.micros(instant(at))


def write(name: str, value: Any) -> None:
    path = OUT / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=None) + "\n", encoding="utf-8")
    print(f"{path.relative_to(REPO)}: {path.stat().st_size} bytes")


# --- floats --------------------------------------------------------------------------------


def floats() -> None:
    values: list[float] = [
        0.0,
        -0.0,
        1.0,
        -1.0,
        0.1,
        0.5,
        1.5,
        2.5,
        1e16,
        1e15,
        1e-4,
        1e-5,
        5e-324,
        2.2250738585072014e-308,
        1.7976931348623157e308,
        math.inf,
        -math.inf,
        math.nan,
        123456789012345678.0,
        9007199254740992.0,
        9007199254740993.0,
        0.3,
        2 / 3,
        math.pi,
    ]
    for k in range(-30, 30):
        p = 10.0**k
        values += [p, math.nextafter(p, 0), math.nextafter(p, math.inf), -p, 1.5 * p, 9.999 * p]
    for _ in range(6000):
        x = struct.unpack("<d", struct.pack("<Q", RNG.getrandbits(64)))[0]
        if not math.isnan(x):
            values.append(x)
    for _ in range(2000):
        values.append(round(RNG.uniform(-180, 180), RNG.randint(0, 9)))
        values.append(float(RNG.randint(-(10**18), 10**18)))
        values.append(RNG.random() * 10.0 ** RNG.randint(-12, 22))
    lines = [f"{bits(x)} {x!r} {json.dumps(x)}" for x in values]
    rounding = []
    for _ in range(1000):
        x = round(RNG.uniform(-180, 180), RNG.randint(5, 12))
        n = RNG.randint(0, 8)
        rounding.append([bits(x), n, bits(round(x, n))])
    hypot = []
    for _ in range(1000):
        a, b = RNG.uniform(-1e3, 1e3) * 10.0 ** RNG.randint(-5, 5), RNG.uniform(-1e3, 1e3)
        hypot.append([bits(a), bits(b), bits(math.hypot(a, b))])
    mod = []
    for _ in range(2000):
        a, b = RNG.uniform(-1000, 1000), RNG.choice([360.0, -360.0, 7.5, 1.0])
        mod.append([bits(a), bits(b), bits(a % b), bits(a // b)])
    mod.append([bits(-0.0), bits(360.0), bits(-0.0 % 360.0), bits(-0.0 // 360.0)])
    (OUT / "pyfloat.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write("pynum.json", {"round": rounding, "hypot": hypot, "mod": mod})


# --- time ----------------------------------------------------------------------------------


def transitions() -> list[list[int]]:
    found = []
    previous = None
    day = datetime(1970, 1, 1, tzinfo=UTC)
    while day.year < 2101:
        offset = int(day.astimezone(times.PRAGUE).utcoffset().total_seconds())
        if previous is not None and offset != previous:
            lo, hi = int((day - timedelta(days=1)).timestamp()), int(day.timestamp())
            while hi - lo > 1:
                mid = (lo + hi) // 2
                at = datetime.fromtimestamp(mid, UTC).astimezone(times.PRAGUE)
                if int(at.utcoffset().total_seconds()) == previous:
                    lo = mid
                else:
                    hi = mid
            found.append([hi, offset])
        previous = offset
        day += timedelta(days=1)
    return found


def zone() -> None:
    data = (Path(tzdata.__file__).parent / "zoneinfo" / "Europe" / "Prague").read_bytes()
    changes = transitions()
    schedule = []
    readings = []
    days = sorted(
        {
            datetime.fromtimestamp(t, UTC).date()
            for t, _ in changes
            if 2019 <= datetime.fromtimestamp(t, UTC).year <= 2045
        }
    )
    days += [date(2026, 10, 8), date(2026, 7, 1), date(2077, 3, 28), date(2077, 10, 31)]
    for day in days:
        for seconds in range(-3600, 31 * 3600, 900):
            schedule.append(
                [day.isoformat(), seconds, times.micros(ServiceTime(day, seconds).instant())]
            )
        for minute in range(0, 24 * 60, 15):
            wall = datetime.combine(day, time()) + timedelta(minutes=minute)
            readings.append([wall.isoformat(), [times.micros(r) for r in times._readings(wall)]])
    walls = []
    for _ in range(1000):
        at = EPOCH + timedelta(microseconds=RNG.randint(0, 4_000_000_000_000_000))
        walls.append(
            [
                us(at),
                times.local_wall(instant(at)).isoformat(),
                times.local_date(instant(at)).isoformat(),
            ]
        )
    write(
        "tz.json",
        {
            "iana_version": tzdata.IANA_VERSION,
            "tzdata_package": tzdata.__version__,
            "tzif_hex": data.hex(),
            "transitions": changes,
            "schedule": schedule,
            "readings": readings,
            "walls": walls,
        },
    )


ISO_CASES = [
    "2026-10-08T22:30:00+00:00",
    "2026-10-08T22:30:00Z",
    "2026-10-08 22:30",
    "2026-10-08T22",
    "2026-10-08",
    "20261008T223000",
    "2026-10-08T22:30:00.1",
    "2026-10-08T22:30:00.1234567",
    "2026-10-08T22:30:00,5",
    "2026-10-08T22:30:00.123+02",
    "2026-10-08T22:30:00+0200",
    "2026-10-08T22:30:00+02:00:30",
    "2026-10-08T22:30:00+02:00:30.5",
    "2026-10-08x22:30",
    "2026-W41-4T10:00",
    "2026W414T10:00",
    "2026-W41",
    "2026W41",
    "2026-W53-1",
    "2020-W53-7",
    "2026-10-08T2230",
    "2026-10-08T24:00:00",
    "2026-10-08T22:30:00.",
    "2026-10-08T22:30:00 +02:00",
    "2026-10-08T22:30:00x+02:00",
    "2026-10-08T22:30:00z",
    "2026-10-08T22:30:00-00:00",
    "2026-10-08T22:30:00.123456789Z",
    "2026-10-08T22:30:60",
    " 2026-10-08",
    "2026-10-08T22:30:00+24:00",
    "2026-10-08T22:30:00+23:59",
    "2026-10-08T22:30:00.12345",
    "2026-10-08T223000.5",
    "2026-1008T10:00",
    "2026-10-08T22:30:00+02:00 ",
    "2026-10-08T22:30:00Z+01:00",
    "2026-10-08T10:00:00-02:30",
    "2026-02-29",
    "2028-02-29T00:00:00+01:00",
    "0001-01-01T00:00:00+01:00",
    "9999-12-31T23:59:59-01:00",
    "2026-10-08T",
    "2026-10-08T22:3",
    "2026-10-08T22:30:0",
    "2026-10-08T22:30.5",
    "2026-13-01",
    "2026-10-32",
    "26-10-08",
    "2026-10-08T22:30:00+2",
    "2026-10-08T22:30:00+02:3",
    "",
    "2026",
    "2026-10-08T22:30:00.000000+00:00",
    "2026-10-08T22:30:00.000001+05:45",
    "2026-10-08Ž22:30",
    "2026-10-0822:30",
    "2026-10-08T22:30:00+02:00:00.000001",
    "2026-10-08T07:05:03.5-11:00",
]


def iso_cases() -> None:
    cases = []
    for text in ISO_CASES:
        entry: dict[str, Any] = {"text": text}
        try:
            parsed = datetime.fromisoformat(text)
            offset = parsed.utcoffset()
            entry["wall"] = parsed.replace(tzinfo=None).isoformat()
            entry["offset_us"] = None if offset is None else offset // timedelta(microseconds=1)
        except ValueError:
            entry["error"] = True
        try:
            entry["date"] = date.fromisoformat(text).isoformat()
        except ValueError:
            entry["date"] = None
        cases.append(entry)
    write("iso.json", cases)


def time_functions() -> None:
    scaled = []
    for _ in range(1500):
        delta = RNG.randint(-(10**12), 10**12)
        factor = RNG.choice(
            [RNG.uniform(-3, 3), RNG.random() * 1e-9, 0.5, -0.5, 1 / 3, 1e-300, 1e3]
        )
        td = timedelta(microseconds=delta)
        scaled.append(
            [
                delta,
                bits(factor),
                (td * factor) // timedelta(microseconds=1),
                times.scaled_micros(delta, factor),
            ]
        )
    divided = []
    for _ in range(2000):
        delta, n = RNG.randint(-(10**12), 10**12), RNG.choice([2, 3, -2, 7, 1000])
        f = RNG.choice([2.0, 0.3, -1.5, 1e-3])
        td = timedelta(microseconds=delta)
        divided.append(
            [
                delta,
                n,
                (td / n) // timedelta(microseconds=1),
                bits(f),
                (td / f) // timedelta(microseconds=1),
                (td // n) // timedelta(microseconds=1),
            ]
        )
    seconds = []
    for _ in range(1500):
        x = RNG.choice(
            [RNG.uniform(-1e5, 1e5), round(RNG.uniform(-100, 100), 7), RNG.uniform(-1, 1) * 1e-6]
        )
        seconds.append([bits(x), timedelta(seconds=x) // timedelta(microseconds=1)])
    for x in [0.5e-6, 1.5e-6, 2.5e-6, -0.5e-6, -2.5e-6, 1e-7, 3600.0, 14400.0, 0.0]:
        seconds.append([bits(x), timedelta(seconds=x) // timedelta(microseconds=1)])
    mids = []
    for _ in range(1000):
        lo = EPOCH + timedelta(microseconds=RNG.randint(0, 2 * 10**15))
        hi = lo + timedelta(microseconds=RNG.randint(0, 10**9))
        mids.append([us(lo), us(hi), times.micros(Interval(instant(lo), instant(hi)).mid)])
    ticks = []
    for _ in range(1000):
        at = EPOCH + timedelta(microseconds=RNG.randint(0, 2 * 10**15))
        every = RNG.choice([1, 10, 60, 3600])
        ticks.append([us(at), every, times.micros(times.next_multiple(instant(at), every))])
    local = []
    layouts = ["%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
    texts = [
        "25.10.2026 02:30:00",
        "29.3.2026 2:30:00",
        "8.10.2026 9:5:7",
        " 8.10.2026  22:30:00 ",
        "31.02.2026 10:00:00",
        "08.10.2026 24:00:00",
        "08.10.2026 10:00:60",
        "2026-10-25T02:30:00",
        "2026-10-25t02:30:00",
        "2026-3-29T02:30:00",
        "garbage",
        "",
        "08.10.2026 10:00:00x",
        "2026-10-08T22:30:00.5",
        "08.10.2026\t10:00:00",
        "8.1.2026 23:59:59",
    ]
    for text in texts:
        for layout in layouts:
            for received in (
                "2026-10-25T00:31:00",
                "2026-10-25T01:31:00",
                "2026-03-29T00:31:00",
                "2026-10-08T20:00:00",
            ):
                at = instant(datetime.fromisoformat(received).replace(tzinfo=UTC))
                found = times.read_local(text, layout, at)
                local.append(
                    [text, layout, times.micros(at), None if found is None else times.micros(found)]
                )
    clocks = []
    for _ in range(1500):
        at = EPOCH + timedelta(
            microseconds=RNG.randint(1_700_000_000_000_000, 1_900_000_000_000_000)
        )
        clock = time(RNG.randint(0, 23), RNG.randint(0, 59), RNG.choice([0, 30]))
        found = times.resolve_clock(clock, instant(at))
        micros = ((clock.hour * 60 + clock.minute) * 60 + clock.second) * 1_000_000
        clocks.append([micros, us(at), None if found is None else times.micros(found)])
    offsets = []
    for text in [
        "2026-07-01T12:00:00+02:00",
        "2026-07-01T10:00:00+01:00",
        "2026-07-01T10:00:00+02:00",
        "2026-07-01T08:00:00+02:00",
        "2026-07-01T08:00:00+00:00",
        "2026-07-01T08:00:00",
        "2026-07-01T09:59:00+01:00",
        "2026-07-01T06:00:00-02:00",
        "2026-12-01T10:04:00+02:00",
    ]:
        for ref in ("2026-07-01T08:00:10", "2026-12-01T08:00:00"):
            reference = instant(datetime.fromisoformat(ref).replace(tzinfo=UTC))
            found = times.read_unless_offset_twice(text, reference, timedelta(minutes=5))
            digits = times.read_digits_as_local(text, reference)
            offsets.append(
                [
                    text,
                    times.micros(reference),
                    None if found is None else times.micros(found),
                    None if digits is None else times.micros(digits),
                ]
            )
    write(
        "times.json",
        {
            "scaled": scaled,
            "divided": divided,
            "seconds": seconds,
            "mids": mids,
            "ticks": ticks,
            "read_local": local,
            "resolve_clock": clocks,
            "offsets": offsets,
        },
    )


# --- geo -----------------------------------------------------------------------------------


def geometry() -> None:
    pairs = []
    for _ in range(1500):
        if RNG.random() < 0.7:
            a = (RNG.uniform(12, 19), RNG.uniform(48.5, 51.1))
            b = (a[0] + RNG.uniform(-0.05, 0.05), a[1] + RNG.uniform(-0.05, 0.05))
        else:
            a = (RNG.uniform(-180, 180), RNG.uniform(-90, 90))
            b = (RNG.uniform(-180, 180), RNG.uniform(-90, 90))
        pairs.append([bits(a[0]), bits(a[1]), bits(b[0]), bits(b[1]), bits(geo.distance_m(a, b))])
    line = tuple(
        (14.0 + i * 0.001 + RNG.uniform(0, 1e-4), 50.0 + RNG.uniform(-1e-3, 1e-3))
        for i in range(60)
    )
    write(
        "geo.json",
        {
            "pairs": pairs,
            "line": [[bits(x), bits(y)] for x, y in line],
            "cumulative": [bits(v) for v in geo.cumulative_m(line)],
        },
    )


# --- config --------------------------------------------------------------------------------


def plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, frozenset):
        return sorted(value)
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def config() -> None:
    write(
        "config.json",
        {
            "policy_file": POLICY.name,
            "policy": plain(load_policy()),
            "sources": [plain(s) for s in load_sources(SOURCES, tuple(FILTERS))],
            "filters": sorted(FILTERS),
        },
    )


# --- facts ---------------------------------------------------------------------------------


def at(text: str) -> Instant:
    return instant(datetime.fromisoformat(text))


def made() -> list[Observation]:
    t0 = at("2026-10-08T22:30:00+00:00")
    t1 = at("2026-10-08T22:30:00.000001+00:00")
    awkward = [
        "",
        "Teplice,Celní",
        'quote " backslash \\ slash /',
        "tab\tnew\nline\r\x00\x1f\x7f",
        "Ústí nad Labem 🚌 €",
        "".join(map(chr, (0x2028, 0x2029, 0xFEFF))),
        "日本",
    ]
    facts: list[Fact] = [
        VehicleKey("405012"),
        TripKey("cis:line_trip", "582492:143"),
        LineRef("cis:line", "580916"),
        Destination(awkward[1]),
        NextStopName(awkward[4]),
        Position(50.66, 14.03, None),
        Position(50.0, 14.0, 0.0),
        Position(-0.0, 1e-05, 359.99999999999994),
        Position(49.123456789012345, 1e16, 1e-7),
        Delay(-60, "unknown"),
        Delay(0, "arrival"),
        Delay(10**12, "departure"),
        Delay(-1800, "point"),
        SourceState("1"),
        SourceState(""),
        SourceDeparture(t0),
        SourceDeparture(t1),
        ServiceDay(date(2026, 10, 8)),
        ServiceDay(date(1, 1, 1)),
        PointEvent("Praha hl.n.", ("54673", "54674"), t0, Interval(t0, t1), True),
        PointEvent("Ústí n.L.", (), None, Interval(t1, t1), False),
        NextPoint("Děčín", "54321"),
        NextPoint("x", None),
        NextStopPrediction("54321", t0, None),
        NextStopPrediction("1", None, t1),
        TripStatus(True, False),
        TripStatus(False, True),
        TripCancelled(),
        PlatformAssignment("54673", "arrival", t0, "1A", "platform"),
        PlatformAssignment("54673", "departure", t1, "3", "track"),
        VehicleAttribute("low_floor", "true"),
    ] + [Destination(text) for text in awkward]
    out = [
        Observation("hand", "made", "jdf", t0, None, RawRef("0" * 64, 0), 1, tuple(facts)),
        Observation("hand", "made", "czptt", t1, t0, RawRef("f" * 64, 7), 3, ()),
        Observation("hand", "made", "czptt", t0, t1, RawRef("a" * 64, 1), 2, (TripCancelled(),)),
    ]
    for i in range(300):
        lat = RNG.choice([RNG.uniform(48, 51), round(RNG.uniform(48, 51), RNG.randint(0, 8))])
        lon = RNG.choice([RNG.uniform(12, 19), round(RNG.uniform(12, 19), 6)])
        bearing = RNG.choice([None, float(RNG.randint(0, 359)), RNG.uniform(0, 360)])
        out.append(
            Observation(
                "hand",
                "random",
                "jdf",
                t0,
                t1,
                RawRef("b" * 64, i),
                1,
                (VehicleKey(str(i)), Position(lat, lon, bearing)),
            )
        )
    return out


def archived() -> list[Observation]:
    policy = load_policy()
    skew = policy.time.max_clock_skew
    found: list[Observation] = []
    seen: set[tuple[str, str]] = set()
    for root in ARCHIVES:
        if not root.is_dir():
            continue
        for (source, channel), decoder in sorted(DECODERS.items()):
            if (source, channel) in seen or not (root / source / channel).is_dir():
                continue
            days = sorted(p.name for p in (root / source / channel).iterdir() if p.is_dir())
            if not days:
                continue
            day = date.fromisoformat(days[0])
            polls = [
                p for p in iter_polls(root, source, channel, day, day) if p.entry.get("sha256")
            ]
            step = max(1, len(polls) // POLLS_PER_CHANNEL)
            channel_obs: list[Observation] = []
            for poll in polls[::step][:POLLS_PER_CHANNEL]:
                body = poll.body()
                request = poll.entry.get("request")
                try:
                    channel_obs += decoder(
                        body,
                        poll.entry["sha256"],
                        instant(poll.received_at),
                        skew,
                        request if isinstance(request, dict) else {},
                    )
                except Exception as error:
                    print(f"  {source}/{channel}: {error}", file=sys.stderr)
            stride = max(1, len(channel_obs) // OBSERVATIONS_PER_CHANNEL)
            found += channel_obs[::stride][:OBSERVATIONS_PER_CHANNEL]
            seen.add((source, channel))
            print(f"  {root.name}: {source}/{channel} {day}: {len(channel_obs)} decoded")
    return found


def facts() -> None:
    observations = made() + archived()
    ints = 0
    lines = []
    for obs in observations:
        for fact in obs.facts:
            if isinstance(fact, Position) and not all(
                isinstance(v, float)
                for v in (fact.lat, fact.lon)
                + ((fact.bearing,) if fact.bearing is not None else ())
            ):
                ints += 1
        document = facts_to_json(obs.facts)
        lines.append(
            json.dumps(
                {
                    "source": obs.source,
                    "channel": obs.channel,
                    "feed": obs.feed,
                    "received_at": obs.received_at.isoformat(),
                    "observed_at": None if obs.observed_at is None else obs.observed_at.isoformat(),
                    "sha256": obs.raw.sha256,
                    "item": obs.raw.item,
                    "decoder_version": obs.decoder_version,
                    "facts": json.dumps(document, separators=(",", ":")),
                    "facts_utf8": json.dumps(document, separators=(",", ":"), ensure_ascii=False),
                },
                ensure_ascii=False,
            )
        )
    (OUT / "facts.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"facts.jsonl: {len(lines)} observations; positions with a non-float coordinate: {ints}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    floats()
    zone()
    iso_cases()
    time_functions()
    geometry()
    config()
    facts()


if __name__ == "__main__":
    main()
