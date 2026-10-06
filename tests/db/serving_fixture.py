"""Tiny serving-v5 release directories for loader tests."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from obehy.pipeline.files import file_digest
from obehy.release.contract import Contract, Relation, load_contract

Rows = dict[str, list[dict[str, Any]]]

ARROW_TYPES: dict[str, pa.DataType] = {
    "string": pa.string(),
    "int16": pa.int16(),
    "int32": pa.int32(),
    "double": pa.float64(),
    "bool": pa.bool_(),
    "date32": pa.date32(),
}


def jdf_rows() -> Rows:
    stop, post, other = "jdf:stop:1", "jdf:stop:1:post:1", "jdf:stop:2"
    trip, route, service = "jdf:trip:1", "jdf:route:000001", "jdf:service:1"
    return {
        "agency": [{"agency_id": "jdf:agency:1", "name": "Dopravce", "timezone": "Europe/Prague"}],
        "location": [
            {
                "location_id": stop,
                "kind": "stop_place",
                "domain": "surface",
                "name": "Česká Lípa,,aut.nádr.",
                "coordinate_precision": "exact",
                "coordinate_source": "osm",
                "longitude": 14.54,
                "latitude": 50.68,
            },
            {
                "location_id": post,
                "kind": "boarding_point",
                "domain": "surface",
                "parent_location_id": stop,
                "name": "Česká Lípa,,aut.nádr.",
                "public_code": "1",
                "coordinate_precision": "estimated",
                "coordinate_source": "route_time",
                "longitude": 14.541,
                "latitude": 50.681,
            },
            {
                "location_id": other,
                "kind": "stop_place",
                "domain": "surface",
                "name": "Nový Bor,,nám.",
                "coordinate_precision": "missing",
            },
        ],
        "route": [
            {
                "route_id": route,
                "agency_id": "jdf:agency:1",
                "mode": "bus",
                "gtfs_route_type": 3,
                "short_name": "1",
                "timetable_kind": "regular",
            }
        ],
        # Monday to Friday 2026-10-05..11, without Wednesday the 7th, plus Saturday the 10th.
        "service_calendar": [
            {
                "service_id": service,
                "valid_from": date(2026, 10, 5),
                "valid_to": date(2026, 10, 11),
                "weekday_mask": 0b0011111,
            }
        ],
        "service_exception": [
            {"service_id": service, "service_date": date(2026, 10, 7), "added": False},
            {"service_id": service, "service_date": date(2026, 10, 10), "added": True},
        ],
        "trip": [
            {
                "trip_id": trip,
                "route_id": route,
                "service_id": service,
                "headsign": "Nový Bor",
                "shape_id": "jdf:shape:1",
            }
        ],
        "trip_call": [
            {
                "trip_id": trip,
                "sequence": 1,
                "location_id": stop,
                "boarding_point_id": post,
                "route_stop_id": "jdf:route_stop:1",
                "passenger_service": True,
                "scheduled_arrival": 6 * 3600,
                "scheduled_departure": 6 * 3600,
                "pickup_type": 0,
                "dropoff_type": 1,
                "timepoint": True,
            },
            {
                "trip_id": trip,
                "sequence": 2,
                "location_id": other,
                "route_stop_id": "jdf:route_stop:2",
                "passenger_service": True,
                "scheduled_arrival": 6 * 3600 + 900,
                "scheduled_departure": 6 * 3600 + 900,
                "pickup_type": 1,
                "dropoff_type": 0,
                "timepoint": True,
            },
        ],
        "route_stop": [
            {
                "route_stop_id": "jdf:route_stop:1",
                "route_id": route,
                "sequence": 1,
                "location_id": stop,
            },
            {
                "route_stop_id": "jdf:route_stop:2",
                "route_id": route,
                "sequence": 2,
                "location_id": other,
            },
        ],
        "route_stop_zone": [
            {"route_stop_id": "jdf:route_stop:1", "source_order": 0, "zone_code": "1"}
        ],
        "call_zone": [{"trip_id": trip, "sequence": 2, "source_order": 0, "zone_code": "2"}],
        "shape": [{"shape_id": "jdf:shape:1", "generation_method": "compiler"}],
        "shape_point": [
            {"shape_id": "jdf:shape:1", "sequence": 1, "longitude": 14.54, "latitude": 50.68},
            {"shape_id": "jdf:shape:1", "sequence": 2, "longitude": 14.40, "latitude": 50.76},
        ],
        "transfer": [
            {
                "transfer_key": "jdf:transfer:1",
                "from_location_id": stop,
                "to_location_id": stop,
                "transfer_type": 2,
                "minimum_transfer_time": 120,
            }
        ],
        "service_note": [
            {
                "note_id": "jdf:note:1",
                "kind": "timetable_note",
                "label": "x",
                "text": "jede přes Sloup",
                "source_object_id": "Caskody:1",
            }
        ],
        "assignment": [
            {
                "assignment_id": "jdf:assignment:1",
                "scope": "trip",
                "kind": "note",
                "trip_id": trip,
                "note_id": "jdf:note:1",
            }
        ],
        "connection_claim": [
            {
                "connection_id": "jdf:connection:1",
                "direction": "waits_for",
                "origin_trip_id": trip,
                "origin_sequence": 1,
                "target_derivation": "none",
                "resolution_status": "unresolved",
                "source_object_id": "Spoje:1",
            }
        ],
        "travel_restriction": [
            {
                "restriction_id": "jdf:restriction:1",
                "scope": "route_stop",
                "route_id": route,
                "source_route_stop_id": "1",
                "route_stop_id": "jdf:route_stop:1",
                "group_code": "§",
                "source_object_id": "Zaslinky:1",
            }
        ],
        "source_key": [
            {
                "entity_kind": "trip",
                "namespace": "cis:line_trip",
                "identifier": "000001:1",
                "public_id": trip,
                "valid_from": date(2026, 10, 5),
                "valid_to": date(2026, 10, 11),
                "binding_method": "identity",
            }
        ],
        "call_key": [
            {
                "namespace": "cis:line_trip",
                "identifier": "000001:1",
                "source_sequence": "3",
                "trip_id": trip,
                "sequence": 2,
            }
        ],
    }


def czptt_rows() -> Rows:
    station, trip, service = "czptt:location:CZ54000", "czptt:trip:1:1", "czptt:service:1"
    return {
        "agency": [{"agency_id": "czptt:agency:1", "name": "ČD", "timezone": "Europe/Prague"}],
        "location": [
            {
                "location_id": station,
                "kind": "stop_place",
                "domain": "heavy_rail",
                "name": "Česká Lípa hl.n.",
                "coordinate_precision": "exact",
                "coordinate_source": "sr70",
                "longitude": 14.537,
                "latitude": 50.683,
            },
            {
                "location_id": "czptt:location:CZ54001",
                "kind": "operational_point",
                "domain": "heavy_rail",
                "name": "Česká Lípa střelnice",
                "coordinate_precision": "exact",
                "coordinate_source": "sr70",
                "longitude": 14.55,
                "latitude": 50.69,
            },
        ],
        "route": [
            {
                "route_id": "czptt:route:1",
                "agency_id": "czptt:agency:1",
                "mode": "rail",
                "gtfs_route_type": 2,
                "timetable_kind": "regular",
            }
        ],
        "service_calendar": [
            {
                "service_id": service,
                "valid_from": date(2026, 10, 5),
                "valid_to": date(2026, 10, 11),
                "weekday_mask": 0,
            }
        ],
        "service_exception": [
            {"service_id": service, "service_date": date(2026, 10, 7), "added": True}
        ],
        "trip": [
            {
                "trip_id": trip,
                "route_id": "czptt:route:1",
                "service_id": service,
                "short_name": "6600",
                "run_key": "Pa:0054:KT----06600A:00:2026",
                "run_part": 1,
            }
        ],
        "trip_call": [
            {
                "trip_id": trip,
                "sequence": 1,
                "location_id": station,
                "passenger_service": True,
                "scheduled_arrival": 6 * 3600 + 300,
                "scheduled_departure": 6 * 3600 + 360,
                "pickup_type": 0,
                "dropoff_type": 0,
                "timepoint": True,
            },
            {
                "trip_id": trip,
                "sequence": 2,
                "location_id": "czptt:location:CZ54001",
                "passenger_service": False,
                "scheduled_arrival": 6 * 3600 + 480,
                "scheduled_departure": 6 * 3600 + 480,
                "pickup_type": 1,
                "dropoff_type": 1,
                "timepoint": True,
            },
        ],
        "source_key": [
            {
                "entity_kind": "trip",
                "namespace": "czptt:train_number",
                "identifier": "6600",
                "public_id": trip,
                "valid_from": date(2026, 10, 5),
                "valid_to": date(2026, 10, 11),
                "binding_method": "identity",
            }
        ],
    }


def _table(relation: Relation, rows: list[dict[str, Any]]) -> pa.Table:
    schema = pa.schema(
        [
            pa.field(field.name, ARROW_TYPES[field.type], nullable=field.nullable)
            for field in relation.fields
        ]
    )
    return pa.Table.from_pylist(rows, schema=schema)


def write_package(
    root: Path,
    rows: Rows,
    *,
    contract: Contract,
    feed_version: str,
    manifest_patch: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Write a package and return its manifest sha256."""

    (root / "serving").mkdir(parents=True)
    (root / "gtfs.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (root / "diagnostics.json").write_text("{}", encoding="utf-8")
    relations: list[dict[str, Any]] = []
    for relation in contract.relations:
        relative = f"serving/{relation.name}.parquet"
        relation_rows = rows.get(relation.name, [])
        pq.write_table(  # pyright: ignore[reportUnknownMemberType]
            _table(relation, relation_rows), root / relative
        )
        relations.append(
            {
                "name": relation.name,
                "path": relative,
                "schema": [
                    {"name": field.name, "type": field.type, "nullable": field.nullable}
                    | ({"enum": field.enum} if field.enum else {})
                    for field in relation.fields
                ],
                "primary_key": list(relation.primary_key),
                "foreign_keys": [],
                "row_count": len(relation_rows),
            }
        )
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": file_digest(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    manifest: dict[str, Any] = {
        "bundle_format": "jrutil-production",
        "bundle_version": 3,
        "serving_schema_version": contract.version,
        "contract_valid": True,
        "publication_eligible": True,
        "feed_version": feed_version,
        "relations": relations,
        "files": files,
    }
    if manifest_patch is not None:
        manifest_patch(manifest)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
    return file_digest(root / "manifest.json")


def write_release(
    root: Path,
    run_id: str,
    *,
    jdf: Rows | None = None,
    czptt: Rows | None = None,
    manifest_patch: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """A release directory like ``obehy build`` publishes, with both packages."""

    contract = load_contract()
    release_dir = root / run_id
    packages: dict[str, Any] = {}
    for name, rows in (("jdf", jdf or jdf_rows()), ("czptt", czptt or czptt_rows())):
        manifest_sha256 = write_package(
            release_dir / name,
            copy.deepcopy(rows),
            contract=contract,
            feed_version=f"sha256:{run_id}:{name}",
            manifest_patch=manifest_patch,
        )
        packages[name] = {"manifest_sha256": manifest_sha256, "package_sha256": "x"}
    release = {
        "schema_version": 1,
        "run_id": run_id,
        "completed_at": "2026-10-06T20:31:42+00:00",
        "gvd_year": 2026,
        "packages": packages,
    }
    (release_dir / "release.json").write_text(json.dumps(release), encoding="utf-8")
    return release_dir
