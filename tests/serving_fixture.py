from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from obehy.serving import (
    NETEX_MAPPING_SHA256,
    RELATIONS,
    canonical_json_bytes,
    schema_document,
    sha256_file,
)

DIGEST = "a" * 64


def fixture_rows() -> dict[str, list[dict[str, Any]]]:
    trip = "v0:trip:0123456789abcdef"
    stop = "v0:stop-place:0123456789abcdef"
    post = "v0:boarding-point:0123456789abcdef"
    operation = "rail:CZ:12345:timing-point-with-a-long-identifier"
    return {
        "agency": [
            {
                "agency_id": "v0:agency:a",
                "name": "Dopravce",
                "url": None,
                "timezone": "Europe/Prague",
                "language": "cs",
                "phone": None,
                "fare_url": None,
                "email": None,
            }
        ],
        "location": [
            {
                "location_id": operation,
                "kind": "operational_point",
                "domain": "heavy_rail",
                "parent_location_id": None,
                "name": "Výhybna",
                "public_code": None,
                "description": None,
                "municipality_name": None,
                "district_name": None,
                "district_code": None,
                "nearby_place": None,
                "country_code": "CZ",
                "coordinate_precision": "source",
                "longitude": 14.2,
                "latitude": 50.1,
                "url": None,
                "timezone": None,
                "wheelchair_boarding": None,
            },
            {
                "location_id": post,
                "kind": "boarding_point",
                "domain": "surface",
                "parent_location_id": stop,
                "name": "Nástupiště A",
                "public_code": "A",
                "description": None,
                "municipality_name": "Lhotka",
                "district_name": "Praha-východ",
                "district_code": "PH",
                "nearby_place": "centrum",
                "country_code": "CZ",
                "coordinate_precision": "source",
                "longitude": 14.1,
                "latitude": 50.0,
                "url": None,
                "timezone": None,
                "wheelchair_boarding": 0,
            },
            {
                "location_id": stop,
                "kind": "stop_place",
                "domain": "surface",
                "parent_location_id": None,
                "name": "Lhotka",
                "public_code": None,
                "description": None,
                "municipality_name": "Lhotka",
                "district_name": "Praha-východ",
                "district_code": "PH",
                "nearby_place": "centrum",
                "country_code": "CZ",
                "coordinate_precision": "source",
                "longitude": 14.1,
                "latitude": 50.0,
                "url": None,
                "timezone": "Europe/Prague",
                "wheelchair_boarding": 0,
            },
        ],
        "route": [
            {
                "route_id": "v0:route:r",
                "agency_id": "v0:agency:a",
                "mode": "bus",
                "gtfs_route_type": 3,
                "short_name": "100",
                "long_name": "Lhotka - Centrum",
                "description": None,
                "url": None,
                "color": "0076A3",
                "text_color": "FFFFFF",
                "sort_order": None,
            }
        ],
        "service_calendar": [
            {
                "service_id": "weekday",
                "valid_from": date(2026, 1, 1),
                "valid_to": date(2026, 12, 31),
                "weekday_mask": 31,
            }
        ],
        "service_exception": [],
        "shape": [{"shape_id": "shape-1", "generation_method": "source"}],
        "shape_point": [
            {
                "shape_id": "shape-1",
                "sequence": 0,
                "longitude": 14.1,
                "latitude": 50.0,
                "distance_traveled": 0.0,
            },
            {
                "shape_id": "shape-1",
                "sequence": 1,
                "longitude": 14.2,
                "latitude": 50.1,
                "distance_traveled": 15000.0,
            },
        ],
        "trip": [
            {
                "trip_id": trip,
                "route_id": "v0:route:r",
                "service_id": "weekday",
                "direction": 0,
                "headsign": "Centrum",
                "short_name": None,
                "block_key": None,
                "wheelchair_accessible": 0,
                "bikes_allowed": 0,
                "shape_id": "shape-1",
            }
        ],
        "trip_call": [
            {
                "trip_id": trip,
                "sequence": 10,
                "location_id": stop,
                "passenger_service": True,
                "boarding_point_id": post,
                "scheduled_arrival": 3600,
                "scheduled_departure": 3660,
                "scheduled_passage": None,
                "pickup_type": 0,
                "dropoff_type": 0,
                "timepoint": True,
                "stop_headsign": None,
                "shape_distance_traveled": 0.0,
            },
            {
                "trip_id": trip,
                "sequence": 20,
                "location_id": operation,
                "passenger_service": False,
                "boarding_point_id": None,
                "scheduled_arrival": None,
                "scheduled_departure": None,
                "scheduled_passage": 4200,
                "pickup_type": 1,
                "dropoff_type": 1,
                "timepoint": True,
                "stop_headsign": None,
                "shape_distance_traveled": 15000.0,
            },
        ],
        "route_segment": [
            {"trip_id": trip, "from_sequence": 10, "to_sequence": 20, "route_id": "v0:route:r"}
        ],
        "transfer": [],
        "fare_system": [{"fare_system_id": "pid", "name": "PID"}],
        "fare_zone": [{"fare_system_id": "pid", "zone_id": "P", "name": "Praha"}],
        "location_zone": [{"location_id": stop, "fare_system_id": "pid", "zone_id": "P"}],
        "call_zone": [
            {
                "trip_id": trip,
                "sequence": 10,
                "fare_system_id": "pid",
                "zone_id": "P",
                "source_order": 0,
            }
        ],
        "service_note": [
            {
                "note_id": "v0:note:reservation",
                "kind": "reservation",
                "label": None,
                "text": "Místenku lze koupit u dopravce.",
                "valid_from": None,
                "valid_to": None,
                "service_note_type": None,
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:notice:reservation:001588:1:1:1",
            },
            {
                "note_id": "v0:note:route",
                "kind": "route_information",
                "label": None,
                "text": "Informace pro cestující.",
                "valid_from": None,
                "valid_to": None,
                "service_note_type": None,
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:notice:route:001588:1:1",
            },
            {
                "note_id": "v0:note:service",
                "kind": "service_note",
                "label": "T",
                "text": "Spoj je nutné objednat předem.",
                "valid_from": date(2026, 1, 1),
                "valid_to": date(2026, 12, 31),
                "service_note_type": "on_request",
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:notice:trip:001588:1:1:1",
            },
        ],
        "service_note_assignment": [
            {
                "assignment_id": "v0:note-assignment:reservation",
                "note_id": "v0:note:reservation",
                "scope": "trip",
                "route_id": None,
                "trip_id": trip,
            },
            {
                "assignment_id": "v0:note-assignment:route",
                "note_id": "v0:note:route",
                "scope": "route",
                "route_id": "v0:route:r",
                "trip_id": None,
            },
            {
                "assignment_id": "v0:note-assignment:service",
                "note_id": "v0:note:service",
                "scope": "trip",
                "route_id": None,
                "trip_id": trip,
            },
        ],
        "service_feature_assignment": [
            {
                "feature_id": "v0:feature:bicycle",
                "scope": "trip",
                "kind": "bicycle_transport",
                "route_id": None,
                "trip_id": trip,
                "call_sequence": None,
                "source_code": "O",
                "note_id": None,
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:trip:001588:1:1:O",
            },
            {
                "feature_id": "v0:feature:on-request",
                "scope": "trip",
                "kind": "on_request",
                "route_id": None,
                "trip_id": trip,
                "call_sequence": None,
                "source_code": "T",
                "note_id": "v0:note:service",
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:trip:001588:1:1:T",
            },
        ],
        "location_feature": [
            {
                "feature_id": "v0:location-feature:rail",
                "location_id": stop,
                "kind": "rail_interchange",
                "source_code": "v",
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:stop:100:v",
            },
            {
                "feature_id": "v0:location-feature:toilet",
                "location_id": stop,
                "kind": "toilet",
                "source_code": "W",
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:stop:100:W",
            },
        ],
        "connection_claim": [
            {
                "connection_id": "v0:connection:unresolved",
                "direction": "connects_to",
                "origin_trip_id": trip,
                "origin_sequence": 10,
                "target_source_route_id": None,
                "target_source_trip_id": None,
                "target_source_stop_id": None,
                "target_source_post_id": None,
                "target_source_end_stop_id": None,
                "target_source_end_post_id": None,
                "wait_minutes": None,
                "note": "Navazuje další spoj.",
                "target_public_line": None,
                "target_destination_text": None,
                "target_derivation": "none",
                "resolution_status": "unresolved",
                "target_route_id": None,
                "target_trip_id": None,
                "target_location_id": None,
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:transfer:001588:1:1:1",
            }
        ],
        "travel_restriction_assignment": [
            {
                "assignment_id": "v0:restriction:trip-call",
                "scope": "trip_call",
                "route_id": None,
                "trip_id": trip,
                "source_route_stop_id": "10",
                "call_sequence": 10,
                "group_code": "§",
                "source_id": "national-jdf",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "jdf:restriction:001588:1:1:10:§",
            }
        ],
        "operational_location": [
            {
                "source_id": "national-czptt",
                "source_location_id": "CZ:12345",
                "source_snapshot_sha256": DIGEST,
                "country_code": "CZ",
                "primary_code": "12345",
                "name": "Výhybna",
                "longitude": None,
                "latitude": None,
                "coordinate_source": None,
                "coordinate_source_object_id": None,
                "coordinate_match_method": None,
            }
        ],
        "operational_journey": [
            {
                "source_id": "national-czptt",
                "source_journey_id": "PA-1",
                "source_snapshot_sha256": DIGEST,
                "domain": "heavy_rail",
                "mode": "rail",
            }
        ],
        "operational_call": [
            {
                "source_id": "national-czptt",
                "source_journey_id": "PA-1",
                "sequence": 20,
                "source_location_id": "CZ:12345",
                "passenger_service": False,
                "scheduled_arrival": None,
                "scheduled_departure": None,
                "scheduled_passage": 4200,
                "subsidiary_code": "1",
                "subsidiary_name": "kolej 1",
                "active_line_code": "L1",
            }
        ],
        "source_entity_map": [
            {
                "source_id": "pid-gtfs",
                "identifier_namespace": "gtfs_stop_id",
                "entity_kind": "boarding_point",
                "source_object_id": "U1Z1P",
                "public_id": post,
            }
        ],
        "source_trip_map": [
            {
                "source_id": "pid-gtfs",
                "trip_namespace": "gtfs_trip_id",
                "source_trip_id": "pid-trip-1",
                "trip_id": trip,
                "valid_from": date(2026, 1, 1),
                "valid_to": date(2026, 12, 31),
                "scheduled_start": 3660,
                "scheduled_end": 4200,
                "source_route_id": "L1",
                "source_direction_id": "0",
                "source_start_location_id": "U1Z1P",
                "source_end_location_id": "U2",
                "source_block_id": "block-1",
                "source_run_id": "run-1",
                "source_duty_id": "duty-1",
                "call_pattern_sha256": "4" * 64,
                "variant_key": "weekday",
            }
        ],
        "source_call_map": [
            {
                "source_id": "pid-gtfs",
                "trip_namespace": "gtfs_trip_id",
                "source_trip_id": "pid-trip-1",
                "call_namespace": "gtfs_stop_sequence",
                "source_sequence": "1",
                "trip_id": trip,
                "call_sequence": 10,
            }
        ],
        "source_trip_coverage": [
            {
                "source_id": "pid-gtfs",
                "coverage_id": "pid-posts-1",
                "trip_namespace": "gtfs_trip_id",
                "source_trip_id": "pid-trip-1",
                "trip_id": trip,
                "from_sequence": 10,
                "to_sequence": 20,
                "coverage_type": "regional",
                "system_id": "pid",
                "coverage_role": "realtime",
            }
        ],
        "identifier_alias": [
            {
                "source_id": "duk",
                "namespace": "cis_line",
                "observed_id": "582588",
                "valid_from": date(2026, 1, 1),
                "valid_to": None,
                "canonical_value": "001588",
                "reason": "DÚK encoding",
            }
        ],
        "road_route_key": [
            {
                "cis_line_id": "001588",
                "route_id": "v0:route:r",
                "valid_from": date(2026, 1, 1),
                "valid_to": date(2026, 12, 31),
            }
        ],
        "road_trip_key": [
            {
                "cis_line_id": "001588",
                "cis_trip_id": 1,
                "trip_id": trip,
                "valid_from": date(2026, 1, 1),
                "valid_to": date(2026, 12, 31),
            }
        ],
        "rail_trip_key": [
            {
                "train_number": 123,
                "trip_id": trip,
                "valid_from": date(2026, 1, 1),
                "valid_to": date(2026, 12, 31),
            }
        ],
        "selected_field_provenance": [
            {
                "object_type": "trip",
                "object_key": trip,
                "field_name": "boarding_point_id",
                "source_id": "pid",
                "source_snapshot_sha256": DIGEST,
                "source_object_id": "pid-trip-1",
                "selection_rule": "posts-authoritative",
            }
        ],
    }


def write_serving_package(
    root: Path, rows: dict[str, list[dict[str, Any]]] | None = None
) -> dict[str, Any]:
    serving = root / "serving"
    serving.mkdir(parents=True)
    all_rows = fixture_rows() if rows is None else rows
    relations: list[dict[str, Any]] = []
    digest_rows: list[dict[str, object]] = []
    for name, spec in RELATIONS.items():
        schema = spec.schema.with_metadata(
            {b"obehy.schema_version": b"1", b"obehy.relation": name.encode()}
        )
        table = pa.Table.from_pylist(all_rows[name], schema=schema)
        path = serving / f"{name}.parquet"
        pq.write_table(table, path, compression="snappy")
        digest = sha256_file(path)
        document = {
            "name": name,
            "path": f"serving/{name}.parquet",
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "row_count": table.num_rows,
            "schema": schema_document(spec.schema),
            "sort_key": list(spec.sort_key),
        }
        relations.append(document)
        digest_rows.append({"name": name, "sha256": digest, "row_count": table.num_rows})
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "identity_contract": "provisional-v0",
        "feed_version": "fixture-v0",
        "build_spec_sha256": DIGEST,
        "source_set_sha256": "b" * 64,
        "overlay_policy_sha256": "c" * 64,
        "compiler_sha256": "d" * 64,
        "compiler_identity": {"name": "fixture-jrutil", "version": "1"},
        "compiler_options_sha256": "e" * 64,
        "registry_snapshot_sha256": None,
        "gtfs_sha256": "f" * 64,
        "extensions_sha256": "1" * 64,
        "diagnostics_sha256": "2" * 64,
        "validation_sha256": "3" * 64,
        "netex_mapping_version": "1",
        "netex_target_schema": "v2.0.0",
        "netex_extension_version": "1",
        "netex_mapping_sha256": NETEX_MAPPING_SHA256,
        "relations": relations,
        "serving_sha256": hashlib.sha256(canonical_json_bytes(digest_rows)).hexdigest(),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
