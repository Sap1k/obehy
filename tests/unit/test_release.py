from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from obehy.release.contract import (
    CONTRACT_PATH,
    ContractError,
    check_declared_schema,
    load_contract,
)
from obehy.release.ddl import MIGRATION, static_ddl, static_tables
from obehy.release.load import LoadError, read_release, verify_package
from obehy.release.migrate import Migration, MigrationError, discover, pending
from obehy.runtime_config import DATABASE_URL_ENV, ConfigurationError, load_database_url

CONTRACT = load_contract()


def _declared() -> list[dict[str, Any]]:
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return [
        {
            "name": relation["name"],
            "schema": relation["fields"],
            "primary_key": relation["primary_key"],
        }
        for relation in document["relations"]
    ]


def test_contract_has_the_v5_relations() -> None:
    assert CONTRACT.version.startswith("5.")
    assert len(CONTRACT.relations) == 19
    trip_call = CONTRACT.relation("trip_call")
    assert trip_call.primary_key == ("trip_id", "sequence")
    assert {field.name: field.sql_type for field in trip_call.fields}["sequence"] == "integer"
    assert "§" in CONTRACT.enumerations["restriction_group"]


def test_checked_in_static_migration_is_generated() -> None:
    assert MIGRATION.read_text(encoding="utf-8") == static_ddl(CONTRACT)


def test_static_tables_add_derived_helpers() -> None:
    tables = {table.name: table for table in static_tables(CONTRACT)}
    assert tables["service_date"].derived
    assert tables["location"].view_columns[-1] == "geom"


def test_declared_schema_accepts_minor_additions() -> None:
    declared = _declared()
    declared[0]["schema"].append({"name": "added_later", "type": "string", "nullable": True})
    declared.append({"name": "new_relation", "schema": [], "primary_key": []})
    check_declared_schema(CONTRACT, declared)


Change = Callable[[list[dict[str, Any]]], object]
MAJOR_CHANGES: list[tuple[Change, str]] = [
    (lambda d: d.pop(), "lacks relation"),
    (lambda d: d[0]["schema"].pop(), "lacks"),
    (lambda d: d[0]["schema"][0].update(type="int32"), "contract says string"),
    (lambda d: d[0]["schema"][2].update(nullable=False), "nullable"),
    (lambda d: d[0].update(primary_key=["name"]), "another primary key"),
]


@pytest.mark.parametrize(("change", "message"), MAJOR_CHANGES)
def test_declared_schema_rejects_major_changes(change: Change, message: str) -> None:
    declared = copy.deepcopy(_declared())
    change(declared)
    with pytest.raises(ContractError, match=message):
        check_declared_schema(CONTRACT, declared)


def test_verify_package_rejects_a_manifest_not_in_release(tmp_path: Path) -> None:
    package = tmp_path / "jdf"
    (package / "serving").mkdir(parents=True)
    (package / "gtfs.zip").write_bytes(b"")
    (package / "diagnostics.json").write_text("{}", encoding="utf-8")
    manifest = {
        "bundle_format": "jrutil-production",
        "bundle_version": 3,
        "serving_schema_version": "5.0",
        "contract_valid": True,
        "publication_eligible": True,
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    release = {"schema_version": 1, "run_id": "r", "packages": {"jdf": {"manifest_sha256": "0"}}}
    (tmp_path / "release.json").write_text(json.dumps(release), encoding="utf-8")

    with pytest.raises(LoadError, match=r"does not match release\.json"):
        verify_package(tmp_path, read_release(tmp_path), "jdf", CONTRACT)
    with pytest.raises(LoadError, match="no czptt package"):
        verify_package(tmp_path, read_release(tmp_path), "czptt", CONTRACT)


def test_serving_v4_package_is_rejected(tmp_path: Path) -> None:
    package = tmp_path / "jdf"
    (package / "serving").mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "bundle_format": "jrutil-production",
                "bundle_version": 3,
                "serving_schema_version": "4.0",
                "contract_valid": True,
            }
        ),
        encoding="utf-8",
    )
    release: dict[str, Any] = {"schema_version": 1, "run_id": "r", "packages": {"jdf": {}}}
    with pytest.raises(LoadError, match="Unsupported"):
        verify_package(tmp_path, release, "jdf", CONTRACT)


def test_shipped_migrations_are_contiguous() -> None:
    assert [migration.name for migration in discover()] == [
        "foundation",
        "control",
        "static",
        "functions",
        "rt",
        "history",
        "rail",
    ]


def test_pending_migrations_detect_edits_and_unknown_versions() -> None:
    migrations = [Migration(1, "a", "", "x"), Migration(2, "b", "", "y")]
    assert pending({1: "x"}, migrations) == [migrations[1]]
    with pytest.raises(MigrationError, match="changed after it was applied"):
        pending({1: "edited"}, migrations)
    with pytest.raises(MigrationError, match="does not know"):
        pending({1: "x", 2: "y", 3: "z"}, migrations)


def test_migration_gaps_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "0003_c.sql").write_text("", encoding="utf-8")
    with pytest.raises(MigrationError, match="without gaps"):
        discover(tmp_path)


def test_database_url_from_config_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    config = tmp_path / "obehy.toml"
    config.write_text(
        'schema_version = 1\n[database]\nurl = "postgresql://u@h/db"\n', encoding="utf-8"
    )
    assert load_database_url(config) == "postgresql://u@h/db"

    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://env@h/db")
    assert load_database_url(config) == "postgresql://env@h/db"

    monkeypatch.delenv(DATABASE_URL_ENV)
    config.write_text("schema_version = 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"\[database\]"):
        load_database_url(config)


def test_migration_checksum_ignores_line_endings(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    lf = discover(tmp_path)[0].sha256
    (tmp_path / "0001_a.sql").write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    assert discover(tmp_path)[0].sha256 == lf
