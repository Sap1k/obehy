from __future__ import annotations

# PyArrow 21 does not ship complete typing information.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import json
from pathlib import Path

import pytest

from obehy.serving import NETEX_MAPPING, RELATIONS, ServingPackageError, validate_serving_package

from ..serving_fixture import fixture_rows, write_serving_package


def test_complete_serving_fixture_validates(tmp_path: Path) -> None:
    write_serving_package(tmp_path)
    package = validate_serving_package(tmp_path)
    assert package.manifest["identity_contract"] == "provisional-v0"
    assert package.relations["trip_call"].row_count == 2
    assert package.relations["source_trip_map"].row_count == 1


def test_changed_relation_is_rejected_by_hash(tmp_path: Path) -> None:
    write_serving_package(tmp_path)
    with (tmp_path / "serving" / "agency.parquet").open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ServingPackageError, match="Size or SHA-256"):
        validate_serving_package(tmp_path)


def test_unsupported_manifest_version_is_rejected(tmp_path: Path) -> None:
    manifest = write_serving_package(tmp_path)
    manifest["schema_version"] = 2
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ServingPackageError, match="schema version"):
        validate_serving_package(tmp_path)


def test_unsorted_relation_is_rejected(tmp_path: Path) -> None:
    rows = fixture_rows()
    rows["location"] = list(reversed(rows["location"]))
    write_serving_package(tmp_path, rows)
    with pytest.raises(ServingPackageError, match="Unsorted key"):
        validate_serving_package(tmp_path)


def test_netex_ledger_covers_typed_semantic_relations() -> None:
    semantic_relations = {
        "service_note",
        "service_note_assignment",
        "service_feature_assignment",
        "location_feature",
        "connection_claim",
        "travel_restriction_assignment",
        "operational_location",
        "operational_journey",
        "operational_call",
    }
    ledger = NETEX_MAPPING["relations"]
    for relation_name in semantic_relations:
        expected = set(RELATIONS[relation_name].schema.names)
        assert set(ledger[relation_name]["source_fields"]) == expected


def test_wrong_netex_mapping_contract_is_rejected(tmp_path: Path) -> None:
    manifest = write_serving_package(tmp_path)
    manifest["netex_mapping_sha256"] = "0" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ServingPackageError, match="mapping contract digest"):
        validate_serving_package(tmp_path)
