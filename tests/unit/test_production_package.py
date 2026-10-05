import json
from pathlib import Path

import pytest

from obehy.production_package import ProductionPackageError, read_manifest, serving_schema_major


def _package(root: Path, version: object) -> Path:
    (root / "serving").mkdir(parents=True)
    (root / "gtfs.zip").write_bytes(b"")
    (root / "diagnostics.json").write_text("{}", encoding="utf-8")
    manifest = {
        "bundle_format": "jrutil-production",
        "bundle_version": 3,
        "serving_schema_version": version,
        "contract_valid": True,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("version", "major"),
    [("5.0", 5), ("5.12", 5), ("4", None), (4, None), ("5", None), ("five.0", None)],
)
def test_serving_schema_major(version: object, major: int | None) -> None:
    assert serving_schema_major(version) == major


def test_read_manifest_accepts_any_minor_of_major_five(tmp_path: Path) -> None:
    assert read_manifest(_package(tmp_path / "a", "5.3"))["serving_schema_version"] == "5.3"


@pytest.mark.parametrize("version", [4, "4.0", "6.0"])
def test_read_manifest_rejects_other_majors(tmp_path: Path, version: object) -> None:
    with pytest.raises(ProductionPackageError):
        read_manifest(_package(tmp_path / "a", version))
