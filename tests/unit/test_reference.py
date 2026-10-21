"""The vehicle registry's datasets: our own list, the DÚK seed, and their checks."""

from __future__ import annotations

from datetime import date

import pytest

from obehy.reference import duk_register, vehicle_list
from obehy.reference.dataset import Key
from obehy.reference.vocabulary import load_vocabulary

VOCABULARY = load_vocabulary()


def test_the_duk_seed_keys_each_vehicle_by_its_duk_id() -> None:
    dataset = duk_register.parse(
        "vhc_id,model,agency,year_of_manufacture,low_floor,contactless_payments,"
        "air_conditioning,alternate_fuel,USB_chargers\n"
        "807,Iveco Crossway LE LINE 13M,AKV bus,2014,1,0,1,0,0\n",
        "v1",
    )
    dataset.check(VOCABULARY)
    (vehicle,) = dataset.vehicles
    assert vehicle.vehicle_id == "duk:807"
    assert dataset.keys == [Key("duk:807", date(2000, 1, 1), source="duk", source_vehicle_id="807")]
    assert ("duk:807", "low_floor", "true") in dataset.attributes
    assert ("duk:807", "usb", "false") in dataset.attributes
    assert dataset.models[0].manufacturer is None  # never guessed from the model name
    assert (dataset.priority, dataset.private) == (10, True)


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
