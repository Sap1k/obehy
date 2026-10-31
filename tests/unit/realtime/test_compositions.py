"""ČD compositions (docs/sources/cd.md): planning the request from what the worker knows of a
train, decoding the answer, and the lookup manifest and broker helpers."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from obehy.realtime import record
from obehy.realtime.compositions import (
    NotAvailable,
    Subject,
    TripView,
    Unit,
    UpstreamRequest,
    ViewCall,
)
from obehy.realtime.emit.compositions import digest
from obehy.realtime.manifest import Applies, ManifestError, Rate, declared_lookups
from obehy.realtime.runtime.lookups import Bucket, fill
from obehy.realtime.sources import cd
from obehy.realtime.times import instant

SAMPLE = Path(__file__).resolve().parents[2] / "realtime" / "compositions" / "cd-607.json"
DAY = date(2026, 10, 20)
NOW = instant(datetime(2026, 10, 20, 9, 0, tzinfo=UTC))


def _view(
    *,
    left: int = 0,
    company: str = "1154",
    feed: str = "czptt",
    day: date = DAY,
    sr70: bool = True,
) -> TripView:
    """Train 607 from Cheb: Cheb, Sokolov, a passing point, Karlovy Vary, Praha hl.n.; the
    first `left` calls left."""

    stations = [("75035", True), ("74013", True), ("74015", False), ("72015", True)]
    calls = [
        ViewCall(n, f"czptt:stop:CZ:{code}", 1, passenger, n < left, {"sr70": code} if sr70 else {})
        for n, (code, passenger) in enumerate(stations)
    ]
    calls.append(ViewCall(4, "czptt:stop:CZ:54700", 1, True, left > 4, {}))
    keys = {"czptt:train_number": "607", "czptt:tr": f"Tr:{company}:--KADR000607:00:2026"}
    return TripView(feed, "czptt:trip:607", day, NOW, keys, tuple(calls))


def _subject(location: str | None = None) -> Subject:
    return Subject("czptt", "czptt:trip:607", DAY, location)


def test_cd_q1_the_station_is_the_country_code_and_the_sr70() -> None:
    planned = cd.plan(_subject(), _view())
    assert isinstance(planned, UpstreamRequest)
    assert planned.cache_key == "607/2026-10-20/5475035"
    assert planned.start.location_id == "czptt:stop:CZ:75035"
    ((name, value),) = planned.params
    assert name == "params"
    assert json.loads(value) == [
        {"TrainNum": "607", "SR70": "5475035", "Date": "20.10.2026", "GetPlan": "false"}
    ]
    url = fill("https://x.invalid/getTrains?trainParams={params}&format=json", planned.params)
    assert parse_qs(urlsplit(url).query)["trainParams"] == [value]


def test_the_plan_asks_from_the_next_passenger_call_never_the_last() -> None:
    # Sokolov left: the passing point is no passenger call, so Karlovy Vary.
    planned = cd.plan(_subject(), _view(left=2))
    assert isinstance(planned, UpstreamRequest) and planned.start.ordinal == 3
    # Only the terminus is ahead: nothing departs from it, so the formation it arrives with,
    # from the last departing call; the same when asked from the terminus (arrivals there).
    for planned in (
        cd.plan(_subject(), _view(left=4)),
        cd.plan(_subject("czptt:stop:CZ:54700"), _view()),
    ):
        assert isinstance(planned, UpstreamRequest) and planned.start.ordinal == 3
    # Asked from a call: from it, even once left.
    planned = cd.plan(_subject("czptt:stop:CZ:74013"), _view(left=3))
    assert isinstance(planned, UpstreamRequest) and planned.start.ordinal == 1
    # Not a passenger call of the trip (the passing point).
    assert isinstance(cd.plan(_subject("czptt:stop:CZ:74015"), _view()), NotAvailable)


def test_the_plan_knows_what_cd_cannot_answer() -> None:
    assert cd.plan(_subject(), _view(sr70=False)) == NotAvailable(
        "czptt:stop:CZ:75035 has no SR70 code"
    )


@pytest.mark.parametrize(
    ("view", "reason"),
    [
        (_view(), None),
        (_view(company="3246"), "not a trip it answers for (czptt:tr)"),
        (_view(day=date(2026, 10, 21)), None),
        (_view(day=date(2026, 10, 25)), "answers only within 1 day(s) of today"),
    ],
)
def test_the_cd_manifest_says_which_trains_it_answers_for(
    view: TripView, reason: str | None
) -> None:
    # ČD's own trains around today (the manifest's `applies`), checked before `plan`.
    (lookup,) = [lookup for lookup in record.load_lookups() if lookup.source == "cd"]
    assert lookup.applies.refusal(view.keys, view.service_date, DAY) == reason


def test_a_lookup_without_applies_answers_for_every_trip() -> None:
    assert Applies().refusal({}, date(2030, 1, 1), DAY) is None


def test_applies_patterns_must_compile(tmp_path: Path) -> None:
    manifest = tmp_path / "x.toml"
    manifest.write_text(
        'source = "x"\nmanifest_version = 1\n[[lookup]]\nname = "composition"\n'
        'kind = "composition"\nfeeds = ["czptt"]\ntimeout_s = 5\nttl_s = 1\n'
        "not_found_ttl_s = 1\nretry_s = 1\nrate = { per_minute = 1, burst = 1 }\n"
        'daily_budget = 1\napplies = { keys = { "czptt:tr" = "(" } }\n'
        '[lookup.request]\nmethod = "GET"\nurl = "https://x.invalid"\n',
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match=r"applies.keys.czptt:tr"):
        declared_lookups(manifest)


def test_cd_decodes_the_coaches_in_train_order() -> None:
    planned = cd.plan(_subject(), _view())
    assert isinstance(planned, UpstreamRequest)
    units = cd.decode(SAMPLE.read_bytes(), planned)
    assert [(u.kind, u.vehicle_type, u.coach_number) for u in units] == [
        ("locomotive", None, None),
        ("coach", "Bdmpz 896", "367"),
        ("coach", "Bmpz 895", "368"),
        ("coach", "Bmpz 895", "369"),
        ("coach", "Bbmpz 897", "370"),
        ("coach", "Ampz 894", "372"),
    ]
    _, bicycles, *_, family, first = units
    assert bicycles.classes == (2,)
    assert bicycles.features == (
        "bicycle_reservation_required",
        "open_saloon",
        "wifi",
        "power_sockets",
    )
    assert family.features == (
        "wheelchair_lift",
        "family_compartment",
        "open_saloon",
        "children_cinema",
        "wifi",
        "power_sockets",
    )
    assert dict(family.texts)["kino"] == "Dětské kino"
    assert first.classes == (1,)
    assert digest(units) == digest(cd.decode(SAMPLE.read_bytes(), planned))


def _units() -> tuple[Unit, ...]:
    planned = cd.plan(_subject(), _view())
    assert isinstance(planned, UpstreamRequest)
    return cd.decode(SAMPLE.read_bytes(), planned)


def test_cd_q3_the_locomotive_is_a_placeholder() -> None:
    locomotive = _units()[0]
    assert (locomotive.kind, locomotive.vehicle_type, locomotive.coach_number) == (
        "locomotive",
        None,
        None,
    )
    assert ("type", "#3937") in locomotive.details  # not a series: kept raw


def test_cd_q4_the_coach_number_star_is_kept() -> None:
    assert all(("number_star", "true") in unit.details for unit in _units()[1:])


def test_cd_q2_and_an_unknown_train() -> None:
    planned = cd.plan(_subject(), _view())
    assert isinstance(planned, UpstreamRequest)
    raw = cd.decode(
        b'({"d":{"SvcDescEnum":[],"Trains":[{"Coachs":[{"Img":"x/_1.svg",'
        b'"Type":"A 1","Num":"5","Services":[{"Img":"bistro"}]}]}]}});',
        planned,
    )
    assert raw[0].features == ("raw:cd:bistro",)
    assert cd.decode(b'({"d":{"SvcDescEnum":[],"Trains":[]}});', planned) == ()
    with pytest.raises(cd.PayloadError):
        cd.decode(b"<html>busy</html>", planned)


LIVE = SAMPLE.parent


def _decode(name: str) -> tuple[Unit, ...]:
    planned = cd.plan(_subject(), _view())
    assert isinstance(planned, UpstreamRequest)
    return cd.decode((LIVE / name).read_bytes(), planned)


def test_cd_q5_a_through_coach_names_where_it_leaves_and_its_next_train() -> None:
    # rj 577 from Praha (asked live 2026-10-31): a couchette and a sleeper to Budapest behind
    # the locomotive, leaving at Břeclav as EN 477 Metropol, then the railjet set.
    units = _decode("cd-577.json")
    assert [u.vehicle_type for u in units[:4]] == [None, "Bcmz 834", "WLABmee 823", "Bmpz 893"]
    couchette, sleeper = units[1], units[2]
    assert couchette.destination == "Budapest-Nyugati pu"
    assert ("direct_coach_leaves_at", "Břeclav") in couchette.details
    assert ("direct_coach_train", "EN 477 Metropol") in couchette.details
    assert ("notes", "Lehátkový úsek Praha hl.n. - Budapest-Nyugati pu.") in couchette.details
    assert couchette.features == ("couchette", "wifi", "power_sockets")
    assert sleeper.features == ("sleeper", "power_sockets") and sleeper.classes == ()
    assert "restaurant" in units[8].features and "business_compartment" in units[9].features
    assert "quiet_zone" in units[5].features
    # CD-Q7: the railjet's control car (`RV_RailJet_Afmpz890`) at the end; coaches have no cab.
    assert ("cab", "true") in units[9].details
    assert [u.kind for u in units[1:]] == ["coach"] * 9
    assert not any(("cab", "true") in u.details for u in units[:9])


def test_cd_a_multiple_unit_is_one_entry_per_car() -> None:
    # Train 860, an InterPanter (asked live 2026-10-31): five cars, no locomotive; `12` is a car
    # with both classes.
    units = _decode("cd-860.json")
    assert [(u.kind, u.vehicle_type, u.coach_number, u.classes) for u in units] == [
        ("unit", "661", "31", (1, 2)),
        ("unit", "662", "32", (1, 2)),
        ("unit", "064", "33", (2,)),
        ("unit", "662", "34", (1, 2)),
        ("unit", "660", "35", (2,)),
    ]
    # CD-Q7: its end cars (`661l`, `660L`) have the cabs.
    assert [("cab", "true") in u.details for u in units] == [True, False, False, False, True]
    assert "refreshments" in units[1].features
    assert "bicycle_reservation_possible" in units[0].features
    assert "wheelchair_accessible" in units[4].features


def test_cd_q7_a_cab_side_named_in_the_pictogram() -> None:
    # Train 1584 (asked live 2026-10-31): a Regionova trio (814 + 014 + 814: `814_l`, `014_M`,
    # `814_R`) and a pair (814 + its control trailer 914: `814_L`, `914_R`).
    units = _decode("cd-1584.json")
    assert [(u.kind, u.vehicle_type, dict(u.details).get("cab")) for u in units] == [
        ("unit", "814.2", "front"),
        ("unit", "014", None),
        ("unit", "814.2", "rear"),
        ("unit", "814", "front"),
        ("unit", "914", "rear"),
    ]
    # Train 9654, a RegioPanter (`651l`, `642`, `650L`): cabs without a side.
    panter = _decode("cd-9654.json")
    assert [(u.kind, dict(u.details).get("cab")) for u in panter] == [
        ("unit", "true"),
        ("unit", None),
        ("unit", "true"),
    ]


def test_cd_q8_an_answer_from_some_stations_is_in_reverse() -> None:
    # R 619 asked from Praha-Holešovice (2026-10-31): the locomotive last, its pictogram
    # `HV_EL.svg` (ČD lowercases only the first entry's name); from Cheb it comes first.
    units = _decode("cd-619-holesovice.json")
    assert [u.coach_number for u in units] == ["367", "368", "369", "370", "372", None]
    assert units[-1].kind == "locomotive"


def test_cd_an_empty_answer_is_not_found() -> None:
    # EN 443 asked after it left Praha (2026-10-31): ČD names the train, with no coaches.
    assert _decode("cd-443-empty.json") == ()


def test_the_cd_lookup_manifest() -> None:
    (source,) = [s for s in record.load_sources() if s.source == "cd"]
    assert source.channels == () and not source.live_feeds
    (lookup,) = source.lookups
    assert (lookup.qualified, lookup.kind, lookup.feeds) == (
        "cd/composition",
        "composition",
        ("czptt",),
    )
    assert lookup.request.headers == {"Referer": "https://www.cd.cz/"}
    assert "{params}" in lookup.request.url and lookup.request.proxied
    assert lookup.rate == Rate(6.0, 3) and lookup.daily_budget == 1000
    assert record.load_lookups() == [lookup] == declared_lookups()
    assert lookup.applies == Applies((("czptt:tr", "^Tr:1154:"),), 1)


def test_a_token_bucket_refills_at_its_rate() -> None:
    bucket = Bucket(Rate(per_minute=6, burst=2))
    assert [bucket.take(0.0), bucket.take(0.0), bucket.take(0.0)] == [True, True, False]
    assert not bucket.take(9.0)
    assert bucket.take(10.0)  # one token per 10 s
