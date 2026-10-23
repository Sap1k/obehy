"""PID's vehicle models (Golemio; `docs/sources/pid.md`): a seed for the registry, built from
Golemio's public vehicle descriptors, one per vehicle, since PID's position feed names only the
kind of vehicle ("autobus"), not its model.

`fetch` asks Golemio which vehicles run now (`/v2/public/vehiclepositions`) and, slowly, the
descriptor of each one it has not asked about lately (`/v2/public/vehiclepositions/{id}
?scopes=vehicle_descriptor`), keeping what it learnt in a CSV: `route_type, fleet_number,
model, wheelchair_accessible, air_conditioning, usb, fetched_at`. Run every night, it comes to
know the vehicles that run on other days too. Only buses, trams and trolleybuses have
descriptors (metro, trains and ferries have none).

`read` makes the CSV a dataset: each vehicle with a model (never one without), known by its
PID key (`<route_type>:<fleet
number>`, PID-Q1), so its ID is `pid:<route_type>:<number>` (its source form), with its model
and equipment. Seeded, they also describe the vehicle where the registry binds another
source's report to it (by a plate in our own list), not only PID's. The operator is not: the
descriptor's is the vehicle's owner, PID's live one the operator running the trip. The model is
kept as written: no manufacturer is guessed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from obehy.reference.dataset import Dataset, Key, Model, Vehicle

NAME = "pid-vehicles"
TITLE = "PID vehicle models (Golemio)"
SOURCE = "pid"
PRIORITY = 20  # a seed, after the DÚK register; our own list wins
SINCE = date(2000, 1, 1)

API = "https://api.golemio.cz/v2/public/vehiclepositions"
# Descriptor field -> our attribute (attributes.toml).
EQUIPMENT = {
    "is_wheelchair_accessible": "wheelchair_accessible",
    "is_air_conditioned": "air_conditioning",
    "has_usb_chargers": "usb",
}
FIELDS = ("route_type", "fleet_number", "model", *EQUIPMENT.values(), "fetched_at")
# GTFS route types with descriptors: a bus, tram or trolleybus fleet number.
MODES = {"0": "tram", "3": "bus", "11": "trolleybus"}
_PUBLIC_ID = re.compile(r"^service-(\d+)-(\d+)$")
_SLUG = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True, slots=True)
class Known:
    route_type: str
    fleet_number: str
    model: str
    # Our attribute -> "true", "false" or "" (not stated).
    equipment: dict[str, str]
    fetched_at: datetime


def _model_id(model: str) -> str:
    return _SLUG.sub("-", model.lower()).strip("-")


def load_known(path: Path) -> dict[tuple[str, str], Known]:
    if not path.exists():
        return {}
    known: dict[tuple[str, str], Known] = {}
    for row in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))):
        item = Known(
            row["route_type"],
            row["fleet_number"],
            row["model"],
            {attribute: row.get(attribute, "") for attribute in EQUIPMENT.values()},
            datetime.fromisoformat(row["fetched_at"]),
        )
        known[(item.route_type, item.fleet_number)] = item
    return known


def write_known(path: Path, known: dict[tuple[str, str], Known]) -> None:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(FIELDS)
    for key in sorted(known, key=lambda k: (int(k[0]), int(k[1]))):
        k = known[key]
        equipment = [k.equipment.get(attribute, "") for attribute in EQUIPMENT.values()]
        writer.writerow(
            (k.route_type, k.fleet_number, k.model, *equipment, k.fetched_at.isoformat())
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(out.getvalue(), encoding="utf-8")
    temporary.replace(path)


def running_now(listing: dict[str, Any]) -> list[tuple[str, str]]:
    """(route type, fleet number) of each bus, tram or trolleybus in a public listing."""

    found: set[tuple[str, str]] = set()
    for feature in cast(list[dict[str, Any]], listing.get("features") or []):
        properties = cast(dict[str, Any], feature.get("properties") or {})
        match = _PUBLIC_ID.match(str(properties.get("vehicle_id") or ""))
        if match and match.group(1) in MODES:
            found.add((match.group(1), match.group(2)))
    return sorted(found, key=lambda k: (int(k[0]), int(k[1])))


def descriptor_of(answer: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """The descriptor's model (`vehicle_type`, manufacturer and model) and equipment, wherever
    the answer holds it."""

    stack: list[object] = [answer]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            mapping = cast(dict[str, object], item)
            descriptor = mapping.get("vehicle_descriptor")
            if isinstance(descriptor, dict):
                d = cast(dict[str, object], descriptor)
                model = d.get("vehicle_type")
                equipment = {
                    ours: ("true" if d[field] else "false")
                    if isinstance(d.get(field), bool)
                    else ""
                    for field, ours in EQUIPMENT.items()
                }
                return (model.strip() if isinstance(model, str) else ""), equipment
            stack.extend(mapping.values())
        elif isinstance(item, list):
            stack.extend(cast(list[object], item))
    return "", {attribute: "" for attribute in EQUIPMENT.values()}


def _get(url: str, key: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"X-Access-Token": key, "User-Agent": "obehy"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return cast(dict[str, Any], json.load(response))


def fetch(
    path: Path,
    key: str,
    *,
    delay_s: float,
    refresh_days: int,
    limit: int,
    log: Callable[[str], None] = print,
    get: Callable[[str, str], dict[str, Any]] = _get,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Ask Golemio for the descriptors of the vehicles running now that `path` does not know
    or knew longer than `refresh_days` ago, at most `limit`, one every `delay_s`. Progress is
    saved as it goes; a refusal (rate limit) ends the run early. Returns how many it asked."""

    known = load_known(path)
    stale = now() - timedelta(days=refresh_days)
    wanted = [
        k for k in running_now(get(API, key)) if k not in known or known[k].fetched_at < stale
    ][:limit]
    log(f"{NAME}: {len(known)} known, asking for {len(wanted)}")
    asked = 0
    for route_type, number in wanted:
        url = f"{API}/{urllib.parse.quote(f'service-{route_type}-{number}')}"
        try:
            answer = get(f"{url}?scopes=vehicle_descriptor", key)
        except urllib.error.HTTPError as error:
            if error.code == 404:  # gone since the listing
                continue
            log(f"{NAME}: stopped at HTTP {error.code}")
            break
        model, equipment = descriptor_of(answer)
        known[(route_type, number)] = Known(route_type, number, model, equipment, now())
        asked += 1
        if asked % 50 == 0:
            write_known(path, known)
        sleep(delay_s)
    write_known(path, known)
    log(f"{NAME}: asked {asked}, {len(known)} known")
    return asked


def read(path: Path) -> Dataset:
    data = path.read_bytes()
    return parse(data.decode("utf-8"), hashlib.sha256(data).hexdigest())


def parse(text: str, version: str) -> Dataset:
    dataset = Dataset(NAME, version, TITLE, private=False, priority=PRIORITY)
    models: dict[str, Model] = {}
    for row in csv.DictReader(io.StringIO(text)):
        route_type, number, model = row["route_type"], row["fleet_number"], row["model"].strip()
        equipment = {a: row[a] for a in EQUIPMENT.values() if row.get(a, "") in ("true", "false")}
        # A vehicle Golemio gives no model for is not seeded at all.
        if route_type not in MODES or not number.isdigit() or not model:
            continue
        model_id = _model_id(model)
        models.setdefault(model_id, Model(model_id, model, mode=MODES[route_type]))
        source_id = f"{route_type}:{number}"
        vehicle_id = f"{SOURCE}:{source_id}"
        dataset.vehicles.append(Vehicle(vehicle_id, model_id, public_label=number))
        dataset.keys.append(Key(vehicle_id, SINCE, source=SOURCE, source_vehicle_id=source_id))
        dataset.attributes.append((vehicle_id, "fleet_number", number))
        dataset.attributes.extend((vehicle_id, a, v) for a, v in sorted(equipment.items()))
    dataset.models.extend(models[k] for k in sorted(models))
    return dataset
