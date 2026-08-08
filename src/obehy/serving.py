from __future__ import annotations

# PyArrow 21 does not ship complete typing information. Keep the boundary typed explicitly below.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.sql import SQL, Identifier
from sqlalchemy import text
from sqlalchemy.orm import Session

from obehy.persistence.builds import STATIC_RELATIONS, BuildService
from obehy.persistence.models import StaticBuildRow

SERVING_SCHEMA_VERSION = 1
SHA256_KEYS = (
    "build_spec_sha256",
    "source_set_sha256",
    "overlay_policy_sha256",
    "compiler_sha256",
    "compiler_options_sha256",
    "gtfs_sha256",
    "extensions_sha256",
    "diagnostics_sha256",
    "validation_sha256",
    "serving_sha256",
    "netex_mapping_sha256",
)
NETEX_MAPPING_VERSION = "1"
NETEX_TARGET_SCHEMA = "v2.0.0"
NETEX_EXTENSION_VERSION = "1"
NETEX_MAPPING_PATH = Path(__file__).with_name("data") / "netex_mapping_v1.json"


class ServingPackageError(RuntimeError):
    pass


def _schema(*fields: tuple[str, pa.DataType, bool]) -> pa.Schema:
    return pa.schema(
        [pa.field(name, data_type, nullable=nullable) for name, data_type, nullable in fields]
    )


@dataclass(frozen=True, slots=True)
class RelationSpec:
    schema: pa.Schema
    sort_key: tuple[str, ...]
    target_columns: tuple[str, ...]


RELATIONS: dict[str, RelationSpec] = {
    "agency": RelationSpec(
        _schema(
            ("agency_id", pa.string(), False),
            ("name", pa.string(), False),
            ("url", pa.string(), True),
            ("timezone", pa.string(), False),
            ("language", pa.string(), True),
            ("phone", pa.string(), True),
            ("fare_url", pa.string(), True),
            ("email", pa.string(), True),
        ),
        ("agency_id",),
        ("agency_id", "name", "url", "timezone", "language", "phone", "fare_url", "email"),
    ),
    "location": RelationSpec(
        _schema(
            ("location_id", pa.string(), False),
            ("kind", pa.string(), False),
            ("domain", pa.string(), False),
            ("parent_location_id", pa.string(), True),
            ("name", pa.string(), False),
            ("public_code", pa.string(), True),
            ("description", pa.string(), True),
            ("municipality_name", pa.string(), True),
            ("district_name", pa.string(), True),
            ("district_code", pa.string(), True),
            ("nearby_place", pa.string(), True),
            ("country_code", pa.string(), True),
            ("coordinate_precision", pa.string(), True),
            ("longitude", pa.float64(), True),
            ("latitude", pa.float64(), True),
            ("url", pa.string(), True),
            ("timezone", pa.string(), True),
            ("wheelchair_boarding", pa.int16(), True),
        ),
        ("location_id",),
        (
            "location_id",
            "kind",
            "domain",
            "parent_location_id",
            "name",
            "public_code",
            "description",
            "municipality_name",
            "district_name",
            "district_code",
            "nearby_place",
            "country_code",
            "coordinate_precision",
            "position",
            "url",
            "timezone",
            "wheelchair_boarding",
        ),
    ),
    "route": RelationSpec(
        _schema(
            ("route_id", pa.string(), False),
            ("agency_id", pa.string(), False),
            ("mode", pa.string(), False),
            ("gtfs_route_type", pa.int32(), False),
            ("short_name", pa.string(), True),
            ("long_name", pa.string(), True),
            ("description", pa.string(), True),
            ("url", pa.string(), True),
            ("color", pa.string(), True),
            ("text_color", pa.string(), True),
            ("sort_order", pa.int32(), True),
        ),
        ("route_id",),
        (
            "route_id",
            "agency_id",
            "mode",
            "gtfs_route_type",
            "short_name",
            "long_name",
            "description",
            "url",
            "color",
            "text_color",
            "sort_order",
        ),
    ),
    "service_calendar": RelationSpec(
        _schema(
            ("service_id", pa.string(), False),
            ("valid_from", pa.date32(), False),
            ("valid_to", pa.date32(), False),
            ("weekday_mask", pa.int16(), False),
        ),
        ("service_id",),
        ("service_id", "valid_from", "valid_to", "weekday_mask"),
    ),
    "service_exception": RelationSpec(
        _schema(
            ("service_id", pa.string(), False),
            ("service_date", pa.date32(), False),
            ("added", pa.bool_(), False),
        ),
        ("service_id", "service_date"),
        ("service_id", "service_date", "added"),
    ),
    "shape": RelationSpec(
        _schema(("shape_id", pa.string(), False), ("generation_method", pa.string(), False)),
        ("shape_id",),
        ("shape_id", "generation_method"),
    ),
    "shape_point": RelationSpec(
        _schema(
            ("shape_id", pa.string(), False),
            ("sequence", pa.int32(), False),
            ("longitude", pa.float64(), False),
            ("latitude", pa.float64(), False),
            ("distance_traveled", pa.float64(), True),
        ),
        ("shape_id", "sequence"),
        ("shape_id", "sequence", "position", "distance_traveled"),
    ),
    "trip": RelationSpec(
        _schema(
            ("trip_id", pa.string(), False),
            ("route_id", pa.string(), False),
            ("service_id", pa.string(), False),
            ("direction", pa.int16(), True),
            ("headsign", pa.string(), True),
            ("short_name", pa.string(), True),
            ("block_key", pa.string(), True),
            ("wheelchair_accessible", pa.int16(), True),
            ("bikes_allowed", pa.int16(), True),
            ("shape_id", pa.string(), True),
        ),
        ("trip_id",),
        (
            "trip_id",
            "route_id",
            "service_id",
            "direction",
            "headsign",
            "short_name",
            "block_key",
            "wheelchair_accessible",
            "bikes_allowed",
            "shape_id",
        ),
    ),
    "trip_call": RelationSpec(
        _schema(
            ("trip_id", pa.string(), False),
            ("sequence", pa.int32(), False),
            ("location_id", pa.string(), False),
            ("passenger_service", pa.bool_(), False),
            ("boarding_point_id", pa.string(), True),
            ("scheduled_arrival", pa.int32(), True),
            ("scheduled_departure", pa.int32(), True),
            ("scheduled_passage", pa.int32(), True),
            ("pickup_type", pa.int16(), False),
            ("dropoff_type", pa.int16(), False),
            ("timepoint", pa.bool_(), False),
            ("stop_headsign", pa.string(), True),
            ("shape_distance_traveled", pa.float64(), True),
        ),
        ("trip_id", "sequence"),
        (
            "trip_id",
            "sequence",
            "location_id",
            "passenger_service",
            "boarding_point_id",
            "scheduled_arrival",
            "scheduled_departure",
            "scheduled_passage",
            "pickup_type",
            "dropoff_type",
            "timepoint",
            "stop_headsign",
            "shape_distance_traveled",
        ),
    ),
    "route_segment": RelationSpec(
        _schema(
            ("trip_id", pa.string(), False),
            ("from_sequence", pa.int32(), False),
            ("to_sequence", pa.int32(), False),
            ("route_id", pa.string(), False),
        ),
        ("trip_id", "from_sequence"),
        ("trip_id", "from_sequence", "to_sequence", "route_id"),
    ),
    "transfer": RelationSpec(
        _schema(
            ("transfer_key", pa.string(), False),
            ("from_location_id", pa.string(), False),
            ("to_location_id", pa.string(), False),
            ("from_route_id", pa.string(), True),
            ("to_route_id", pa.string(), True),
            ("from_trip_id", pa.string(), True),
            ("to_trip_id", pa.string(), True),
            ("transfer_type", pa.int16(), False),
            ("minimum_transfer_time", pa.int32(), True),
        ),
        ("transfer_key",),
        (
            "transfer_key",
            "from_location_id",
            "to_location_id",
            "from_route_id",
            "to_route_id",
            "from_trip_id",
            "to_trip_id",
            "transfer_type",
            "minimum_transfer_time",
        ),
    ),
    "fare_system": RelationSpec(
        _schema(("fare_system_id", pa.string(), False), ("name", pa.string(), False)),
        ("fare_system_id",),
        ("fare_system_id", "name"),
    ),
    "fare_zone": RelationSpec(
        _schema(
            ("fare_system_id", pa.string(), False),
            ("zone_id", pa.string(), False),
            ("name", pa.string(), True),
        ),
        ("fare_system_id", "zone_id"),
        ("fare_system_id", "zone_id", "name"),
    ),
    "location_zone": RelationSpec(
        _schema(
            ("location_id", pa.string(), False),
            ("fare_system_id", pa.string(), False),
            ("zone_id", pa.string(), False),
        ),
        ("location_id", "fare_system_id", "zone_id"),
        ("location_id", "fare_system_id", "zone_id"),
    ),
    "call_zone": RelationSpec(
        _schema(
            ("trip_id", pa.string(), False),
            ("sequence", pa.int32(), False),
            ("fare_system_id", pa.string(), False),
            ("zone_id", pa.string(), False),
            ("source_order", pa.int32(), False),
        ),
        ("trip_id", "sequence", "fare_system_id", "zone_id"),
        ("trip_id", "sequence", "fare_system_id", "zone_id", "source_order"),
    ),
    "service_note": RelationSpec(
        _schema(
            ("note_id", pa.string(), False),
            ("kind", pa.string(), False),
            ("label", pa.string(), True),
            ("text", pa.string(), True),
            ("valid_from", pa.date32(), True),
            ("valid_to", pa.date32(), True),
            ("service_note_type", pa.string(), True),
            ("source_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("source_object_id", pa.string(), False),
        ),
        ("note_id",),
        (
            "note_id",
            "kind",
            "label",
            "text",
            "valid_from",
            "valid_to",
            "service_note_type",
            "source_id",
            "source_snapshot_sha256",
            "source_object_id",
        ),
    ),
    "service_note_assignment": RelationSpec(
        _schema(
            ("assignment_id", pa.string(), False),
            ("note_id", pa.string(), False),
            ("scope", pa.string(), False),
            ("route_id", pa.string(), True),
            ("trip_id", pa.string(), True),
        ),
        ("assignment_id",),
        ("assignment_id", "note_id", "scope", "route_id", "trip_id"),
    ),
    "service_feature_assignment": RelationSpec(
        _schema(
            ("feature_id", pa.string(), False),
            ("scope", pa.string(), False),
            ("kind", pa.string(), False),
            ("route_id", pa.string(), True),
            ("trip_id", pa.string(), True),
            ("call_sequence", pa.int32(), True),
            ("source_code", pa.string(), False),
            ("note_id", pa.string(), True),
            ("source_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("source_object_id", pa.string(), False),
        ),
        ("feature_id",),
        (
            "feature_id",
            "scope",
            "kind",
            "route_id",
            "trip_id",
            "call_sequence",
            "source_code",
            "note_id",
            "source_id",
            "source_snapshot_sha256",
            "source_object_id",
        ),
    ),
    "location_feature": RelationSpec(
        _schema(
            ("feature_id", pa.string(), False),
            ("location_id", pa.string(), False),
            ("kind", pa.string(), False),
            ("source_code", pa.string(), False),
            ("source_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("source_object_id", pa.string(), False),
        ),
        ("feature_id",),
        (
            "feature_id",
            "location_id",
            "kind",
            "source_code",
            "source_id",
            "source_snapshot_sha256",
            "source_object_id",
        ),
    ),
    "connection_claim": RelationSpec(
        _schema(
            ("connection_id", pa.string(), False),
            ("direction", pa.string(), False),
            ("origin_trip_id", pa.string(), False),
            ("origin_sequence", pa.int32(), False),
            ("target_source_route_id", pa.string(), True),
            ("target_source_trip_id", pa.string(), True),
            ("target_source_stop_id", pa.string(), True),
            ("target_source_post_id", pa.string(), True),
            ("target_source_end_stop_id", pa.string(), True),
            ("target_source_end_post_id", pa.string(), True),
            ("wait_minutes", pa.int32(), True),
            ("note", pa.string(), True),
            ("target_public_line", pa.string(), True),
            ("target_destination_text", pa.string(), True),
            ("target_derivation", pa.string(), False),
            ("resolution_status", pa.string(), False),
            ("target_route_id", pa.string(), True),
            ("target_trip_id", pa.string(), True),
            ("target_location_id", pa.string(), True),
            ("source_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("source_object_id", pa.string(), False),
        ),
        ("connection_id",),
        (
            "connection_id",
            "direction",
            "origin_trip_id",
            "origin_sequence",
            "target_source_route_id",
            "target_source_trip_id",
            "target_source_stop_id",
            "target_source_post_id",
            "target_source_end_stop_id",
            "target_source_end_post_id",
            "wait_minutes",
            "note",
            "target_public_line",
            "target_destination_text",
            "target_derivation",
            "resolution_status",
            "target_route_id",
            "target_trip_id",
            "target_location_id",
            "source_id",
            "source_snapshot_sha256",
            "source_object_id",
        ),
    ),
    "travel_restriction_assignment": RelationSpec(
        _schema(
            ("assignment_id", pa.string(), False),
            ("scope", pa.string(), False),
            ("route_id", pa.string(), True),
            ("trip_id", pa.string(), True),
            ("source_route_stop_id", pa.string(), False),
            ("call_sequence", pa.int32(), True),
            ("group_code", pa.string(), False),
            ("source_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("source_object_id", pa.string(), False),
        ),
        ("assignment_id",),
        (
            "assignment_id",
            "scope",
            "route_id",
            "trip_id",
            "source_route_stop_id",
            "call_sequence",
            "group_code",
            "source_id",
            "source_snapshot_sha256",
            "source_object_id",
        ),
    ),
    "operational_location": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("source_location_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("country_code", pa.string(), False),
            ("primary_code", pa.string(), False),
            ("name", pa.string(), False),
            ("longitude", pa.float64(), True),
            ("latitude", pa.float64(), True),
            ("coordinate_source", pa.string(), True),
            ("coordinate_source_object_id", pa.string(), True),
            ("coordinate_match_method", pa.string(), True),
        ),
        ("source_id", "source_location_id"),
        (
            "source_id",
            "source_location_id",
            "source_snapshot_sha256",
            "country_code",
            "primary_code",
            "name",
            "position",
            "coordinate_source",
            "coordinate_source_object_id",
            "coordinate_match_method",
        ),
    ),
    "operational_journey": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("source_journey_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("domain", pa.string(), False),
            ("mode", pa.string(), False),
        ),
        ("source_id", "source_journey_id"),
        ("source_id", "source_journey_id", "source_snapshot_sha256", "domain", "mode"),
    ),
    "operational_call": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("source_journey_id", pa.string(), False),
            ("sequence", pa.int32(), False),
            ("source_location_id", pa.string(), False),
            ("passenger_service", pa.bool_(), False),
            ("scheduled_arrival", pa.int32(), True),
            ("scheduled_departure", pa.int32(), True),
            ("scheduled_passage", pa.int32(), True),
            ("subsidiary_code", pa.string(), True),
            ("subsidiary_name", pa.string(), True),
            ("active_line_code", pa.string(), True),
        ),
        ("source_id", "source_journey_id", "sequence"),
        (
            "source_id",
            "source_journey_id",
            "sequence",
            "source_location_id",
            "passenger_service",
            "scheduled_arrival",
            "scheduled_departure",
            "scheduled_passage",
            "subsidiary_code",
            "subsidiary_name",
            "active_line_code",
        ),
    ),
    "source_entity_map": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("identifier_namespace", pa.string(), False),
            ("entity_kind", pa.string(), False),
            ("source_object_id", pa.string(), False),
            ("public_id", pa.string(), False),
        ),
        ("source_id", "identifier_namespace", "entity_kind", "source_object_id"),
        (
            "source_id",
            "identifier_namespace",
            "entity_kind",
            "source_object_id",
            "public_id",
        ),
    ),
    "source_trip_map": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("trip_namespace", pa.string(), False),
            ("source_trip_id", pa.string(), False),
            ("trip_id", pa.string(), False),
            ("valid_from", pa.date32(), False),
            ("valid_to", pa.date32(), False),
            ("scheduled_start", pa.int32(), True),
            ("scheduled_end", pa.int32(), True),
            ("source_route_id", pa.string(), True),
            ("source_direction_id", pa.string(), True),
            ("source_start_location_id", pa.string(), True),
            ("source_end_location_id", pa.string(), True),
            ("source_block_id", pa.string(), True),
            ("source_run_id", pa.string(), True),
            ("source_duty_id", pa.string(), True),
            ("call_pattern_sha256", pa.string(), True),
            ("variant_key", pa.string(), True),
        ),
        ("source_id", "trip_namespace", "source_trip_id", "trip_id", "valid_from"),
        (
            "source_id",
            "trip_namespace",
            "source_trip_id",
            "trip_id",
            "valid_from",
            "valid_to",
            "scheduled_start",
            "scheduled_end",
            "source_route_id",
            "source_direction_id",
            "source_start_location_id",
            "source_end_location_id",
            "source_block_id",
            "source_run_id",
            "source_duty_id",
            "call_pattern_sha256",
            "variant_key",
        ),
    ),
    "source_call_map": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("trip_namespace", pa.string(), False),
            ("source_trip_id", pa.string(), False),
            ("call_namespace", pa.string(), False),
            ("source_sequence", pa.string(), False),
            ("trip_id", pa.string(), False),
            ("call_sequence", pa.int32(), False),
        ),
        (
            "source_id",
            "trip_namespace",
            "source_trip_id",
            "call_namespace",
            "source_sequence",
            "trip_id",
            "call_sequence",
        ),
        (
            "source_id",
            "trip_namespace",
            "source_trip_id",
            "call_namespace",
            "source_sequence",
            "trip_id",
            "call_sequence",
        ),
    ),
    "source_trip_coverage": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("coverage_id", pa.string(), False),
            ("trip_namespace", pa.string(), False),
            ("source_trip_id", pa.string(), False),
            ("trip_id", pa.string(), False),
            ("from_sequence", pa.int32(), False),
            ("to_sequence", pa.int32(), False),
            ("coverage_type", pa.string(), False),
            ("system_id", pa.string(), True),
            ("coverage_role", pa.string(), True),
        ),
        ("source_id", "coverage_id", "trip_id", "from_sequence"),
        (
            "source_id",
            "coverage_id",
            "trip_namespace",
            "source_trip_id",
            "trip_id",
            "from_sequence",
            "to_sequence",
            "coverage_type",
            "system_id",
            "coverage_role",
        ),
    ),
    "identifier_alias": RelationSpec(
        _schema(
            ("source_id", pa.string(), False),
            ("namespace", pa.string(), False),
            ("observed_id", pa.string(), False),
            ("valid_from", pa.date32(), False),
            ("valid_to", pa.date32(), True),
            ("canonical_value", pa.string(), False),
            ("reason", pa.string(), False),
        ),
        ("source_id", "namespace", "observed_id", "valid_from"),
        (
            "source_id",
            "namespace",
            "observed_id",
            "valid_from",
            "valid_to",
            "canonical_value",
            "reason",
        ),
    ),
    "road_route_key": RelationSpec(
        _schema(
            ("cis_line_id", pa.string(), False),
            ("route_id", pa.string(), False),
            ("valid_from", pa.date32(), False),
            ("valid_to", pa.date32(), False),
        ),
        ("cis_line_id", "route_id", "valid_from"),
        ("cis_line_id", "route_id", "valid_from", "valid_to"),
    ),
    "road_trip_key": RelationSpec(
        _schema(
            ("cis_line_id", pa.string(), False),
            ("cis_trip_id", pa.int64(), False),
            ("trip_id", pa.string(), False),
            ("valid_from", pa.date32(), False),
            ("valid_to", pa.date32(), False),
        ),
        ("cis_line_id", "cis_trip_id", "trip_id", "valid_from"),
        ("cis_line_id", "cis_trip_id", "trip_id", "valid_from", "valid_to"),
    ),
    "rail_trip_key": RelationSpec(
        _schema(
            ("train_number", pa.int32(), False),
            ("trip_id", pa.string(), False),
            ("valid_from", pa.date32(), False),
            ("valid_to", pa.date32(), False),
        ),
        ("train_number", "trip_id", "valid_from"),
        ("train_number", "trip_id", "valid_from", "valid_to"),
    ),
    "selected_field_provenance": RelationSpec(
        _schema(
            ("object_type", pa.string(), False),
            ("object_key", pa.string(), False),
            ("field_name", pa.string(), False),
            ("source_id", pa.string(), False),
            ("source_snapshot_sha256", pa.string(), False),
            ("source_object_id", pa.string(), False),
            ("selection_rule", pa.string(), False),
        ),
        ("object_type", "object_key", "field_name"),
        (
            "object_type",
            "object_key",
            "field_name",
            "source_id",
            "source_snapshot_sha256",
            "source_object_id",
            "selection_rule",
        ),
    ),
}

if tuple(RELATIONS) != STATIC_RELATIONS:
    raise RuntimeError("Serving contract and static partition inventory differ")


def schema_document(schema: pa.Schema) -> list[dict[str, object]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


with NETEX_MAPPING_PATH.open(encoding="utf-8") as mapping_file:
    NETEX_MAPPING = cast(dict[str, Any], json.load(mapping_file))
NETEX_MAPPING_SHA256 = hashlib.sha256(canonical_json_bytes(NETEX_MAPPING)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ValidatedRelation:
    name: str
    path: Path
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedServingPackage:
    root: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    relations: dict[str, ValidatedRelation]


def _sha256(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ServingPackageError(f"{label} must be a lowercase SHA-256 digest")
    return value


def validate_serving_package(root: Path) -> ValidatedServingPackage:
    root = root.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ServingPackageError(f"Cannot read serving manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise ServingPackageError("Serving manifest must be a JSON object")
    if manifest.get("schema_version") != SERVING_SCHEMA_VERSION:
        raise ServingPackageError("Unsupported serving manifest schema version")
    if manifest.get("identity_contract") not in {"provisional-v0", "registry-v1"}:
        raise ServingPackageError("Unsupported identity contract")
    if not isinstance(manifest.get("feed_version"), str) or not manifest["feed_version"]:
        raise ServingPackageError("feed_version must be non-empty text")
    for key in SHA256_KEYS:
        _sha256(manifest.get(key), key)
    registry = _sha256(
        manifest.get("registry_snapshot_sha256"), "registry_snapshot_sha256", nullable=True
    )
    if manifest["identity_contract"] == "registry-v1" and registry is None:
        raise ServingPackageError("registry-v1 requires registry_snapshot_sha256")
    if not isinstance(manifest.get("compiler_identity"), dict):
        raise ServingPackageError("compiler_identity must be an object")
    if manifest.get("netex_mapping_version") != NETEX_MAPPING_VERSION:
        raise ServingPackageError("Unsupported NeTEx mapping version")
    if manifest.get("netex_target_schema") != NETEX_TARGET_SCHEMA:
        raise ServingPackageError("Unsupported NeTEx target schema")
    if manifest.get("netex_extension_version") != NETEX_EXTENSION_VERSION:
        raise ServingPackageError("Unsupported Czech NeTEx extension version")
    if manifest.get("netex_mapping_sha256") != NETEX_MAPPING_SHA256:
        raise ServingPackageError("Unsupported NeTEx mapping contract digest")

    relation_documents = manifest.get("relations")
    if not isinstance(relation_documents, list):
        raise ServingPackageError("relations must be an array")
    by_name: dict[str, ValidatedRelation] = {}
    digest_rows: list[dict[str, object]] = []
    for document in relation_documents:
        if not isinstance(document, dict) or not isinstance(document.get("name"), str):
            raise ServingPackageError("Every relation entry must name a relation")
        name = document["name"]
        spec = RELATIONS.get(name)
        if spec is None or name in by_name:
            raise ServingPackageError(f"Unknown or duplicate serving relation {name!r}")
        if document.get("schema") != schema_document(spec.schema):
            raise ServingPackageError(f"Manifest schema mismatch for {name}")
        if document.get("sort_key") != list(spec.sort_key):
            raise ServingPackageError(f"Manifest sort key mismatch for {name}")
        relative = document.get("path")
        if not isinstance(relative, str):
            raise ServingPackageError(f"Relation {name} has no path")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or pure.parts != ("serving", f"{name}.parquet"):
            raise ServingPackageError(f"Relation {name} path is not canonical")
        path = root.joinpath(*pure.parts).resolve()
        if root not in path.parents:
            raise ServingPackageError(f"Relation {name} escapes package root")
        expected_hash = _sha256(document.get("sha256"), f"relations[{name}].sha256")
        expected_size = document.get("size_bytes")
        expected_count = document.get("row_count")
        if not isinstance(expected_size, int) or expected_size < 0:
            raise ServingPackageError(f"Relation {name} has invalid size")
        if not isinstance(expected_count, int) or expected_count < 0:
            raise ServingPackageError(f"Relation {name} has invalid row count")
        try:
            stat = path.stat()
        except OSError as error:
            raise ServingPackageError(f"Missing relation {name}: {error}") from error
        if stat.st_size != expected_size or sha256_file(path) != expected_hash:
            raise ServingPackageError(f"Size or SHA-256 mismatch for relation {name}")
        parquet = pq.ParquetFile(path)
        if parquet.schema_arrow != spec.schema:
            raise ServingPackageError(f"Parquet schema mismatch for relation {name}")
        metadata = parquet.schema_arrow.metadata or {}
        if (
            metadata.get(b"obehy.schema_version") != b"1"
            or metadata.get(b"obehy.relation") != name.encode()
        ):
            raise ServingPackageError(f"Parquet metadata mismatch for relation {name}")
        if parquet.metadata.num_rows != expected_count:
            raise ServingPackageError(f"Parquet row-count mismatch for relation {name}")
        previous: tuple[object, ...] | None = None
        actual_count = 0
        for batch in parquet.iter_batches(batch_size=65_536, columns=list(spec.sort_key)):
            for row in batch.to_pylist():
                key = tuple(row[column] for column in spec.sort_key)
                if any(value is None for value in key):
                    raise ServingPackageError(f"Null sort key in relation {name}")
                if previous is not None and key <= previous:
                    kind = "duplicate" if key == previous else "unsorted"
                    raise ServingPackageError(
                        f"{kind.capitalize()} key in relation {name}: {key!r}"
                    )
                previous = key
                actual_count += 1
        if actual_count != expected_count:
            raise ServingPackageError(f"Scanned row-count mismatch for relation {name}")
        relation = ValidatedRelation(name, path, expected_count, cast(str, expected_hash))
        by_name[name] = relation
        digest_rows.append({"name": name, "sha256": expected_hash, "row_count": expected_count})

    if tuple(by_name) != tuple(RELATIONS):
        missing = sorted(set(RELATIONS) - set(by_name))
        raise ServingPackageError(
            f"Serving manifest relation order/set mismatch; missing={missing}"
        )
    serving_digest = hashlib.sha256(canonical_json_bytes(digest_rows)).hexdigest()
    if serving_digest != manifest["serving_sha256"]:
        raise ServingPackageError("Aggregate serving-package digest mismatch")
    return ValidatedServingPackage(root, manifest, sha256_file(manifest_path), by_name)


def _row_values(name: str, row: dict[str, Any]) -> tuple[object, ...]:
    if name in {"location", "operational_location"}:
        longitude = row.pop("longitude")
        latitude = row.pop("latitude")
        if (longitude is None) != (latitude is None):
            raise ServingPackageError("Location coordinates must be both null or both present")
        row["position"] = None if longitude is None else f"SRID=4326;POINT({longitude} {latitude})"
    elif name == "shape_point":
        longitude = row.pop("longitude")
        latitude = row.pop("latitude")
        row["position"] = f"SRID=4326;POINT({longitude} {latitude})"
    return tuple(row[column] for column in RELATIONS[name].target_columns)


class ServingPackageLoader:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load(self, build_id: int, package: ValidatedServingPackage) -> dict[str, int]:
        lifecycle = BuildService(self.session)
        build = self.session.get(StaticBuildRow, build_id)
        if build is None:
            raise ServingPackageError(f"Unknown build {build_id}")
        self._verify_build(build, package)
        lifecycle.mark_loading(build_id)
        staging = self.session.begin_nested()
        try:
            counts = self._load_after_mark_loading(build_id, package)
        except Exception as error:
            staging.rollback()
            lifecycle.mark_failed(
                build_id,
                {"type": type(error).__name__, "message": str(error)},
            )
            raise
        staging.commit()
        return counts

    def _load_after_mark_loading(
        self, build_id: int, package: ValidatedServingPackage
    ) -> dict[str, int]:
        lifecycle = BuildService(self.session)
        driver = cast(
            psycopg.Connection[Any], self.session.connection().connection.driver_connection
        )
        counts: dict[str, int] = {}
        for name, relation in package.relations.items():
            partition = f"{name}_b{build_id}"
            self.session.execute(
                text(
                    f'CREATE TABLE static."{partition}" '
                    f'(LIKE static."{name}" INCLUDING DEFAULTS INCLUDING GENERATED '
                    f"INCLUDING IDENTITY INCLUDING CONSTRAINTS)"
                )
            )
            columns = ("build_id", *RELATIONS[name].target_columns)
            statement = SQL("COPY {}.{} ({}) FROM STDIN").format(
                Identifier("static"),
                Identifier(partition),
                SQL(", ").join(Identifier(column) for column in columns),
            )
            copied = 0
            with driver.cursor().copy(statement) as copy:
                parquet = pq.ParquetFile(relation.path)
                for batch in parquet.iter_batches(batch_size=65_536):
                    for raw in batch.to_pylist():
                        copy.write_row((build_id, *_row_values(name, raw)))
                        copied += 1
            if copied != relation.row_count:
                raise ServingPackageError(f"COPY count mismatch for {name}")
            counts[name] = copied
        self._validate_staging(build_id)
        for name in RELATIONS:
            partition = f"{name}_b{build_id}"
            self.session.execute(
                text(
                    f'ALTER TABLE static."{name}" ATTACH PARTITION static."{partition}" '
                    f"FOR VALUES IN ({build_id})"
                )
            )
        self.session.execute(
            text(
                """
                UPDATE static.shape target
                   SET geometry = lines.geometry
                  FROM (
                        SELECT shape_id, ST_MakeLine(position ORDER BY sequence) AS geometry
                          FROM static.shape_point
                         WHERE build_id = :build_id
                         GROUP BY shape_id
                  ) lines
                 WHERE target.build_id = :build_id AND target.shape_id = lines.shape_id
                """
            ),
            {"build_id": build_id},
        )
        build = self.session.get(StaticBuildRow, build_id)
        if build is None:
            raise ServingPackageError(f"Build {build_id} disappeared during loading")
        build.partitions_attached = True
        self.session.flush()
        lifecycle.add_validation(
            build_id,
            validator="serving-package-v1",
            passed=True,
            report={"relations": counts, "manifest_sha256": package.manifest_sha256},
        )
        lifecycle.mark_ready(build_id)
        return counts

    @staticmethod
    def _verify_build(build: StaticBuildRow, package: ValidatedServingPackage) -> None:
        manifest = package.manifest
        comparisons = {
            "feed_version": build.feed_version,
            "identity_contract": build.identity_contract,
            "build_spec_sha256": build.build_spec_sha256,
            "source_set_sha256": build.source_set_sha256,
            "overlay_policy_sha256": build.overlay_policy_sha256,
            "compiler_sha256": build.compiler_sha256,
            "compiler_options_sha256": build.compiler_options_sha256,
            "registry_snapshot_sha256": build.registry_snapshot_sha256,
            "gtfs_sha256": build.gtfs_sha256,
            "serving_sha256": build.serving_sha256,
            "netex_mapping_version": build.netex_mapping_version,
            "netex_target_schema": build.netex_target_schema,
            "netex_extension_version": build.netex_extension_version,
            "netex_mapping_sha256": build.netex_mapping_sha256,
        }
        for key, expected in comparisons.items():
            if manifest.get(key) != expected:
                raise ServingPackageError(f"Build/manifest mismatch for {key}")
        if package.manifest_sha256 != build.manifest_sha256:
            raise ServingPackageError("Build/manifest digest mismatch")

    def _validate_staging(self, build_id: int) -> None:
        def table(name: str) -> str:
            return f'static."{name}_b{build_id}"'

        checks = {
            "location parent kind/domain": f"""
                SELECT count(*) FROM {table("location")} child
                LEFT JOIN {table("location")} parent
                  ON parent.build_id=child.build_id AND parent.location_id=child.parent_location_id
                WHERE child.kind='boarding_point'
                  AND (parent.location_id IS NULL
                       OR parent.kind<>'stop_place'
                       OR parent.domain<>child.domain)
            """,
            "trip call shape": f"""
                SELECT count(*) FROM {table("trip_call")}
                WHERE (passenger_service
                       AND (boarding_point_id IS NULL OR scheduled_passage IS NOT NULL))
                   OR (NOT passenger_service AND (boarding_point_id IS NOT NULL
                       OR scheduled_arrival IS NOT NULL OR scheduled_departure IS NOT NULL
                       OR pickup_type<>1 OR dropoff_type<>1))
            """,
            "boarding point kind": f"""
                SELECT count(*) FROM {table("trip_call")} call
                JOIN {table("location")} location
                  ON location.build_id=call.build_id AND location.location_id=call.boarding_point_id
                WHERE location.kind<>'boarding_point'
            """,
            "coverage endpoints": f"""
                SELECT count(*) FROM {table("source_trip_coverage")} coverage
                LEFT JOIN {table("trip_call")} first_call
                  ON first_call.build_id=coverage.build_id AND first_call.trip_id=coverage.trip_id
                 AND first_call.sequence=coverage.from_sequence
                LEFT JOIN {table("trip_call")} last_call
                  ON last_call.build_id=coverage.build_id AND last_call.trip_id=coverage.trip_id
                 AND last_call.sequence=coverage.to_sequence
                WHERE first_call.trip_id IS NULL OR last_call.trip_id IS NULL
            """,
            "coverage source trip mapping": f"""
                SELECT count(*) FROM {table("source_trip_coverage")} coverage
                LEFT JOIN {table("source_trip_map")} mapping
                  ON mapping.build_id=coverage.build_id
                 AND mapping.source_id=coverage.source_id
                 AND mapping.trip_namespace=coverage.trip_namespace
                 AND mapping.source_trip_id=coverage.source_trip_id
                 AND mapping.trip_id=coverage.trip_id
                WHERE mapping.trip_id IS NULL
            """,
            "source call trip mapping": f"""
                SELECT count(*) FROM {table("source_call_map")} call_mapping
                LEFT JOIN {table("source_trip_map")} trip_mapping
                  ON trip_mapping.build_id=call_mapping.build_id
                 AND trip_mapping.source_id=call_mapping.source_id
                 AND trip_mapping.trip_namespace=call_mapping.trip_namespace
                 AND trip_mapping.source_trip_id=call_mapping.source_trip_id
                 AND trip_mapping.trip_id=call_mapping.trip_id
                WHERE trip_mapping.trip_id IS NULL
            """,
            "overlapping route segments": f"""
                SELECT count(*) FROM {table("route_segment")} left_segment
                JOIN {table("route_segment")} right_segment
                  ON left_segment.build_id=right_segment.build_id
                 AND left_segment.trip_id=right_segment.trip_id
                 AND left_segment.from_sequence<right_segment.from_sequence
                 AND left_segment.to_sequence>=right_segment.from_sequence
            """,
            "empty service note": f"""
                SELECT count(*) FROM {table("service_note")}
                WHERE label IS NULL AND text IS NULL
            """,
            "service feature references": f"""
                SELECT count(*) FROM {table("service_feature_assignment")} feature
                LEFT JOIN {table("route")} route
                  ON route.build_id=feature.build_id AND route.route_id=feature.route_id
                LEFT JOIN {table("trip")} trip
                  ON trip.build_id=feature.build_id AND trip.trip_id=feature.trip_id
                LEFT JOIN {table("trip_call")} call
                  ON call.build_id=feature.build_id AND call.trip_id=feature.trip_id
                 AND call.sequence=feature.call_sequence
                LEFT JOIN {table("service_note")} note
                  ON note.build_id=feature.build_id AND note.note_id=feature.note_id
                WHERE (feature.scope='route' AND route.route_id IS NULL)
                   OR (feature.scope='trip' AND trip.trip_id IS NULL)
                   OR (feature.scope='call' AND call.trip_id IS NULL)
                   OR (feature.note_id IS NOT NULL AND note.note_id IS NULL)
            """,
            "location feature references": f"""
                SELECT count(*) FROM {table("location_feature")} feature
                LEFT JOIN {table("location")} location
                  ON location.build_id=feature.build_id
                 AND location.location_id=feature.location_id
                WHERE location.location_id IS NULL
            """,
            "connection resolution": f"""
                SELECT count(*) FROM {table("connection_claim")}
                WHERE (resolution_status='resolved' AND target_trip_id IS NULL)
                   OR (resolution_status='pattern' AND target_route_id IS NULL)
                   OR (resolution_status='unresolved'
                       AND (target_trip_id IS NOT NULL OR target_route_id IS NOT NULL))
                   OR (target_derivation='none'
                       AND (target_public_line IS NOT NULL
                            OR target_destination_text IS NOT NULL
                            OR target_source_route_id IS NOT NULL
                            OR target_source_trip_id IS NOT NULL
                            OR target_source_stop_id IS NOT NULL
                            OR target_source_post_id IS NOT NULL
                            OR target_source_end_stop_id IS NOT NULL
                            OR target_source_end_post_id IS NOT NULL))
            """,
            "connection target references": f"""
                SELECT count(*) FROM {table("connection_claim")} claim
                LEFT JOIN {table("route")} route
                  ON route.build_id=claim.build_id AND route.route_id=claim.target_route_id
                LEFT JOIN {table("trip")} trip
                  ON trip.build_id=claim.build_id AND trip.trip_id=claim.target_trip_id
                LEFT JOIN {table("location")} location
                  ON location.build_id=claim.build_id
                 AND location.location_id=claim.target_location_id
                WHERE (claim.target_route_id IS NOT NULL AND route.route_id IS NULL)
                   OR (claim.target_trip_id IS NOT NULL AND trip.trip_id IS NULL)
                   OR (claim.target_location_id IS NOT NULL AND location.location_id IS NULL)
            """,
            "travel restriction references": f"""
                SELECT count(*) FROM {table("travel_restriction_assignment")} restriction
                LEFT JOIN {table("route")} route
                  ON route.build_id=restriction.build_id AND route.route_id=restriction.route_id
                LEFT JOIN {table("trip_call")} call
                  ON call.build_id=restriction.build_id AND call.trip_id=restriction.trip_id
                 AND call.sequence=restriction.call_sequence
                WHERE (restriction.scope='route_stop' AND route.route_id IS NULL)
                   OR (restriction.scope='trip_call' AND call.trip_id IS NULL)
            """,
        }
        for label, statement in checks.items():
            count = self.session.scalar(text(statement))
            if count:
                raise ServingPackageError(f"Set-wise serving validation failed ({label}): {count}")
