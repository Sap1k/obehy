"""Oběhy's own vehicle list: the registry's main dataset, built up by hand and reviewed in git
(`jrunify-ext-geodata/vehicles/vehicles.csv`), winning over seeds such as the DÚK register.

One CSV row per key of a vehicle:

    vehicle_id,source,source_vehicle_id,licence_plate,valid_from,valid_to,
    manufacturer,model,mode,<attribute>,...

- `vehicle_id` is ours, opaque and never recycled; several rows with one ID give the vehicle
  several keys (a plate and a source's fleet ID, or a plate that changed on a date).
- A row's keys are `source` + `source_vehicle_id`, and each identifier it gives:
  `licence_plate`, `evn`, `series_number` (written any way; compared as the vehicle vocabulary
  says). An identifier is also the vehicle's curated attribute. `valid_from` defaults to
  2000-01-01, `valid_to` is open when empty.
- Any other column named in `data/vehicles/attributes.toml` (or `raw:<source>:<field>`) is an
  attribute; a vehicle's rows may repeat a value but never contradict it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date
from pathlib import Path

from obehy.reference.dataset import Dataset, Key, Model, Vehicle
from obehy.reference.vocabulary import RAW_PREFIX, load_vocabulary

NAME = "vehicles"
TITLE = "Oběhy vehicle list"
PRIORITY = 0
SINCE = date(2000, 1, 1)
STRUCTURE = {
    "vehicle_id",
    "source",
    "source_vehicle_id",
    "valid_from",
    "valid_to",
    "manufacturer",
    "model",
    "mode",
}
_SLUG = re.compile(r"[^0-9a-z]+")


def read(path: Path) -> Dataset:
    data = path.read_bytes()
    return parse(data.decode("utf-8-sig"), hashlib.sha256(data).hexdigest())


def _date(value: str, line: int) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{NAME} line {line}: bad date {value!r}") from None


def parse(text: str, version: str) -> Dataset:
    vocabulary = load_vocabulary()
    reader = csv.DictReader(io.StringIO(text))
    columns = [c for c in reader.fieldnames or [] if c not in STRUCTURE]
    for column in columns:
        if column not in vocabulary.attributes and not column.startswith(RAW_PREFIX):
            raise ValueError(f"{NAME}: column {column!r} is not a vehicle attribute")
    if "vehicle_id" not in (reader.fieldnames or []):
        raise ValueError(f"{NAME}: no vehicle_id column")
    dataset = Dataset(NAME, version, TITLE, private=False, priority=PRIORITY)
    models: dict[str, Model] = {}
    vehicles: dict[str, Vehicle] = {}
    attributes: dict[tuple[str, str], str] = {}
    for line, row in enumerate(reader, start=2):

        def cell(name: str, row: dict[str, str] = row) -> str:
            return (row.get(name) or "").strip()

        vehicle_id = cell("vehicle_id")
        if not vehicle_id:
            raise ValueError(f"{NAME} line {line}: no vehicle_id")
        model_id = None
        if cell("model"):
            manufacturer = cell("manufacturer") or None
            name = f"{manufacturer or ''} {cell('model')}".strip()
            model_id = _SLUG.sub("-", name.lower()).strip("-")
            models.setdefault(
                model_id, Model(model_id, cell("model"), manufacturer, cell("mode") or None)
            )
        known = vehicles.get(vehicle_id)
        if (
            known is not None
            and model_id is not None
            and known.vehicle_model_id not in (None, model_id)
        ):
            raise ValueError(f"{NAME} line {line}: {vehicle_id} has two models")
        if known is None or known.vehicle_model_id is None:
            vehicles[vehicle_id] = Vehicle(vehicle_id, model_id)
        since = _date(cell("valid_from"), line) or SINCE
        until = _date(cell("valid_to"), line)
        if cell("source") or cell("source_vehicle_id"):
            if not (cell("source") and cell("source_vehicle_id")):
                raise ValueError(f"{NAME} line {line}: source and source_vehicle_id go together")
            dataset.keys.append(
                Key(vehicle_id, since, until, cell("source"), cell("source_vehicle_id"))
            )
        for scheme in vocabulary.identifiers:
            if cell(scheme):
                identifier = vocabulary.normalise(scheme, cell(scheme))
                dataset.keys.append(
                    Key(vehicle_id, since, until, scheme=scheme, identifier=identifier)
                )
        for column in columns:
            value = cell(column)
            if not value:
                continue
            previous = attributes.setdefault((vehicle_id, column), value)
            if previous != value:
                raise ValueError(
                    f"{NAME} line {line}: {vehicle_id} {column} {previous!r} vs {value!r}"
                )
    dataset.models.extend(models[k] for k in sorted(models))
    dataset.vehicles.extend(vehicles[k] for k in sorted(vehicles))
    dataset.attributes.extend((v, a, x) for (v, a), x in sorted(attributes.items()))
    return dataset
