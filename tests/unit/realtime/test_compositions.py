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
from obehy.realtime.manifest import Rate
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
    # Only the terminus is ahead: nothing departs from it.
    assert cd.plan(_subject(), _view(left=4)) == NotAvailable(
        "the trip has left its last departing call"
    )
    # Asked from a call: from it, even once left.
    planned = cd.plan(_subject("czptt:stop:CZ:74013"), _view(left=3))
    assert isinstance(planned, UpstreamRequest) and planned.start.ordinal == 1
    assert isinstance(cd.plan(_subject("czptt:stop:CZ:54700"), _view()), NotAvailable)


@pytest.mark.parametrize(
    ("view", "reason"),
    [
        (_view(company="3246"), "not a ČD train"),
        (_view(feed="jdf"), "ČD answers trains only"),
        (_view(day=date(2026, 10, 25)), "ČD answers only for trains around today"),
        (_view(sr70=False), "czptt:stop:CZ:75035 has no SR70 code"),
    ],
)
def test_the_plan_knows_what_cd_cannot_answer(view: TripView, reason: str) -> None:
    assert cd.plan(_subject(), view) == NotAvailable(reason)


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
    assert "{params}" in lookup.request.url and lookup.request.egress is None
    assert lookup.rate == Rate(6.0, 3) and lookup.daily_budget == 1000
    assert record.load_lookups() == [lookup]


def test_a_token_bucket_refills_at_its_rate() -> None:
    bucket = Bucket(Rate(per_minute=6, burst=2))
    assert [bucket.take(0.0), bucket.take(0.0), bucket.take(0.0)] == [True, True, False]
    assert not bucket.take(9.0)
    assert bucket.take(10.0)  # one token per 10 s
