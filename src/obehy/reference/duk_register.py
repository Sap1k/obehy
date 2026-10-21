"""The DÚK vehicle register (private; `docs/sources/duk.md`): a seed for the registry, one CSV
row per vehicle keyed by the vehicle ID DÚK's feed sends.

`vhc_id, model, agency, year_of_manufacture, low_floor, contactless_payments,
air_conditioning, alternate_fuel, USB_chargers`. Each row becomes a vehicle known by its DÚK
key, with the ID `duk:<vhc_id>` (the same as its source form, so it reads the same bound or
not); our own vehicle list may describe the same vehicle under that ID and wins where they
differ. The model is kept as written: no manufacturer is guessed from it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date
from pathlib import Path

from obehy.reference.dataset import Dataset, Key, Model, Vehicle
from obehy.reference.vocabulary import flag

NAME = "duk-register"
TITLE = "DÚK vehicle register"
SOURCE = "duk"
PRIORITY = 10  # a seed: our own list wins
# The register states what holds now; its keys are open from the start of the source.
SINCE = date(2000, 1, 1)

_FLAGS = {
    "low_floor": "low_floor",
    "contactless_payments": "contactless_payment",
    "air_conditioning": "air_conditioning",
    "alternate_fuel": "alternative_fuel",
    "USB_chargers": "usb",
}
_SLUG = re.compile(r"[^0-9a-z]+")


def _model_id(model: str) -> str:
    return _SLUG.sub("-", model.lower()).strip("-")


def read(path: Path) -> Dataset:
    data = path.read_bytes()
    return parse(data.decode("utf-8-sig"), hashlib.sha256(data).hexdigest())


def parse(text: str, version: str) -> Dataset:
    dataset = Dataset(NAME, version, TITLE, private=True, priority=PRIORITY)
    models: dict[str, Model] = {}
    for row in csv.DictReader(io.StringIO(text)):
        vhc_id = (row.get("vhc_id") or "").strip()
        if not vhc_id.isdigit():
            raise ValueError(f"{NAME}: bad vhc_id {vhc_id!r}")
        vehicle_id = f"{SOURCE}:{vhc_id}"
        model = (row.get("model") or "").strip()
        model_id = None
        if model:
            model_id = _model_id(model)
            models.setdefault(model_id, Model(model_id, model, mode="bus"))
        dataset.vehicles.append(Vehicle(vehicle_id, model_id, public_label=vhc_id))
        dataset.keys.append(Key(vehicle_id, SINCE, source=SOURCE, source_vehicle_id=vhc_id))
        dataset.attributes.append((vehicle_id, "fleet_number", vhc_id))
        operator = (row.get("agency") or "").strip()
        if operator:
            dataset.attributes.append((vehicle_id, "operator", operator))
        year = (row.get("year_of_manufacture") or "").strip()
        if year.isdigit():
            dataset.attributes.append((vehicle_id, "year", year))
        for field, attribute in _FLAGS.items():
            value = flag(row.get(field, ""))
            if value is not None:
                dataset.attributes.append((vehicle_id, attribute, value))
    dataset.models.extend(models[k] for k in sorted(models))
    return dataset
