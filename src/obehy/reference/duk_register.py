"""The DÚK vehicle register (`docs/sources/duk.md`): a seed for the registry, curated in git as
`jrunify-ext-geodata/vehicles/duk.csv` in our own vehicle list's format (`vehicle_list`), one row
per vehicle keyed by the vehicle ID DÚK's feed sends.

Each vehicle has the ID `duk:<vhc_id>` (the same as its source form, so it reads the same bound
or not); our own vehicle list may describe the same vehicle under that ID and wins where they
differ. The file was normalised from DÚK's own export (`vhc_id, model, agency, …`): models split
into manufacturer and model only where the name starts with a known make, never guessed.
"""

from __future__ import annotations

from pathlib import Path

from obehy.reference import vehicle_list
from obehy.reference.dataset import Dataset

NAME = "duk-register"
TITLE = "DÚK vehicle register"
PRIORITY = 10  # a seed: our own list wins


def read(path: Path) -> Dataset:
    return vehicle_list.read_as(path, NAME, TITLE, PRIORITY)


def parse(text: str, version: str) -> Dataset:
    return vehicle_list.parse(text, version, NAME, TITLE, PRIORITY)
