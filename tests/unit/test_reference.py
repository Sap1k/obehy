"""The vehicle registry's datasets: our own list, the DÚK and PID seeds, and their checks."""

from __future__ import annotations

import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from obehy.reference import duk_register, pid_vehicles, vehicle_list
from obehy.reference.dataset import Key
from obehy.reference.vocabulary import load_vocabulary

VOCABULARY = load_vocabulary()


def test_the_duk_seed_keys_each_vehicle_by_its_duk_id() -> None:
    # jrunify-ext-geodata/vehicles/duk.csv: our own list's format, read as a seed.
    dataset = duk_register.parse(
        "vehicle_id,source,source_vehicle_id,valid_from,valid_to,manufacturer,model,mode,"
        "fleet_number,operator,year,low_floor,contactless_payment,air_conditioning,"
        "alternative_fuel,usb\n"
        "duk:807,duk,807,,,Iveco,Crossway LE LINE 13M,bus,807,AKV bus,2014,true,false,true,"
        "false,false\n",
        "v1",
    )
    dataset.check(VOCABULARY)
    (vehicle,) = dataset.vehicles
    assert vehicle.vehicle_id == "duk:807"
    assert dataset.keys == [Key("duk:807", date(2000, 1, 1), source="duk", source_vehicle_id="807")]
    assert ("duk:807", "low_floor", "true") in dataset.attributes
    assert ("duk:807", "usb", "false") in dataset.attributes
    (model,) = dataset.models
    assert (model.manufacturer, model.model, model.mode) == ("Iveco", "Crossway LE LINE 13M", "bus")
    assert ("duk:807", "operator", "AKV bus") in dataset.attributes
    assert (dataset.name, dataset.priority, dataset.private) == ("duk-register", 10, False)


def test_our_list_adds_a_plate_to_a_seeded_vehicle_and_a_new_one() -> None:
    dataset = vehicle_list.parse(
        "vehicle_id,source,source_vehicle_id,licence_plate,valid_from,valid_to,manufacturer,"
        "model,mode,low_floor,raw:dpmd:Garaz\n"
        "duk:807,,,4u1 23-45,,,,,,,\n"
        "obehy:1,dpmd,834,9U5 1792,2024-01-01,,MAN,Lion's City 12,bus,true,Děčín\n",
        "v1",
    )
    dataset.check(VOCABULARY)
    assert [(k.vehicle_id, k.source, k.scheme, k.identifier) for k in dataset.keys] == [
        ("duk:807", None, "licence_plate", "4U12345"),
        ("obehy:1", "dpmd", None, None),
        ("obehy:1", None, "licence_plate", "9U51792"),
    ]
    assert ("duk:807", "licence_plate", "4u1 23-45") in dataset.attributes
    assert dataset.models[0].vehicle_model_id == "man-lion-s-city-12"
    assert ("obehy:1", "raw:dpmd:Garaz", "Děčín") in dataset.attributes
    assert (dataset.priority, dataset.private) == (0, False)


@pytest.mark.parametrize(
    ("csv", "message"),
    [
        ("vehicle_id,colour\nobehy:1,red\n", "not a vehicle attribute"),
        ("vehicle_id,source\nobehy:1,duk\n", "go together"),
        ("vehicle_id,low_floor\nobehy:1,true\nobehy:1,false\n", "vs"),
        ("vehicle_id,low_floor\nobehy:1,maybe\n", "true or false"),
        ("vehicle_id,valid_from,licence_plate\nobehy:1,2026-13-01,1A\n", "bad date"),
    ],
)
def test_a_list_that_contradicts_itself_does_not_load(csv: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        vehicle_list.parse(csv, "v1").check(VOCABULARY)


def test_identifiers_compare_as_the_vocabulary_says() -> None:
    assert VOCABULARY.normalise("licence_plate", " 4u1-23 45 ") == "4U12345"
    assert VOCABULARY.normalise("series_number", "650.002") == "650002"
    assert VOCABULARY.normalise("evn", "94 54 1 650 002-5") == "945416500025"


def _descriptor(model: str | None, wheelchair: bool | None = True) -> dict[str, Any]:
    return {
        "features": [
            {
                "properties": {
                    "vehicle_descriptor": {
                        "operator": "DP PRAHA",
                        "vehicle_type": model,
                        "is_wheelchair_accessible": wheelchair,
                        "is_air_conditioned": False,
                        "has_usb_chargers": None,
                    }
                }
            }
        ]
    }


def test_the_pid_seed_lists_buses_trams_and_trolleybuses_running_now() -> None:
    listing = {
        "features": [
            {"properties": {"vehicle_id": "service-3-7621"}},
            {"properties": {"vehicle_id": "service-0-9279"}},
            {"properties": {"vehicle_id": "service-11-113"}},
            {"properties": {"vehicle_id": "metro-C-6-6"}},
            {"properties": {"vehicle_id": "train-977"}},
            {"properties": {"vehicle_id": "service-4-306"}},  # a ferry: no descriptor
            {"properties": {"vehicle_id": "service-3-7621"}},
        ]
    }
    assert pid_vehicles.running_now(listing) == [("0", "9279"), ("3", "7621"), ("11", "113")]
    model, equipment = pid_vehicles.descriptor_of(_descriptor("Škoda 15T3"))
    assert model == "Škoda 15T3"
    assert equipment == {"wheelchair_accessible": "true", "air_conditioning": "false", "usb": ""}


def test_the_pid_seed_keys_each_vehicle_by_its_pid_key_with_model_and_equipment() -> None:
    dataset = pid_vehicles.parse(
        "route_type,fleet_number,model,wheelchair_accessible,air_conditioning,usb,fetched_at\n"
        "3,7621,Iveco Crossway LE CITY 12M,true,true,,2026-10-23T08:00:00+00:00\n"
        "0,9279,Škoda 15T3,true,false,false,2026-10-23T08:00:00+00:00\n"
        "0,8328,,true,false,false,2026-10-23T08:00:00+00:00\n"
        "3,1,,,,,2026-10-23T08:00:00+00:00\n",
        "v1",
    )
    dataset.check(VOCABULARY)
    # Without a model a vehicle is not seeded, whatever else Golemio says of it.
    assert [v.vehicle_id for v in dataset.vehicles] == ["pid:3:7621", "pid:0:9279"]
    assert dataset.keys[0] == Key(
        "pid:3:7621", date(2000, 1, 1), source="pid", source_vehicle_id="3:7621"
    )
    assert [(m.model, m.manufacturer, m.mode) for m in dataset.models] == [
        ("Iveco Crossway LE CITY 12M", None, "bus"),
        ("Škoda 15T3", None, "tram"),
    ]
    assert ("pid:3:7621", "air_conditioning", "true") in dataset.attributes
    assert ("pid:0:9279", "usb", "false") in dataset.attributes
    assert not any(a == "usb" for v, a, _ in dataset.attributes if v == "pid:3:7621")
    assert not any(a == "operator" for _, a, _ in dataset.attributes)
    assert (dataset.priority, dataset.private) == (20, False)


def test_the_pid_seed_asks_only_for_vehicles_it_does_not_know_lately(tmp_path: Path) -> None:
    path = tmp_path / "pid-vehicles.csv"
    now = datetime(2026, 10, 23, 8, tzinfo=UTC)
    pid_vehicles.write_known(
        path,
        {
            ("3", "1"): pid_vehicles.Known("3", "1", "Old", {}, now - timedelta(days=2)),
            ("3", "2"): pid_vehicles.Known("3", "2", "Older", {}, now - timedelta(days=40)),
        },
    )
    asked: list[str] = []

    def get(url: str, key: str) -> dict[str, Any]:
        assert key == "k"
        if url == pid_vehicles.API:
            ids = ["service-3-1", "service-3-2", "service-3-3", "service-3-4", "service-3-5"]
            return {"features": [{"properties": {"vehicle_id": i}} for i in ids]}
        asked.append(url)
        if "service-3-4" in url:
            raise urllib.error.HTTPError(url, 404, "gone", {}, None)  # type: ignore[arg-type]
        if "service-3-5" in url:
            raise urllib.error.HTTPError(url, 429, "slow down", {}, None)  # type: ignore[arg-type]
        return _descriptor("New")

    count = pid_vehicles.fetch(
        path,
        "k",
        delay_s=0,
        refresh_days=30,
        limit=10,
        log=lambda _: None,
        get=get,
        sleep=lambda _: None,
        now=lambda: now,
    )
    # 3:1 is known lately; 3:2 is stale; 3:3 is new; 3:4 went away; 3:5 was refused (stop).
    assert [u.split("/")[-1].split("?")[0] for u in asked] == [
        "service-3-2",
        "service-3-3",
        "service-3-4",
        "service-3-5",
    ]
    assert count == 2
    known = pid_vehicles.load_known(path)
    assert {k: v.model for k, v in known.items()} == {
        ("3", "1"): "Old",
        ("3", "2"): "New",
        ("3", "3"): "New",
    }
    assert known[("3", "3")].equipment["wheelchair_accessible"] == "true"
