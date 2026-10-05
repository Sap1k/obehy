"""Helpers for JrUtil's closed production-package contract."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from obehy.pipeline.files import file_digest

SERVING_SCHEMA_MAJOR = 5


class ProductionPackageError(RuntimeError):
    """A JrUtil production package is malformed or cannot be published."""


def serving_schema_major(version: object) -> int | None:
    """The major of a ``major.minor`` serving schema version; any minor is accepted."""

    if not isinstance(version, str):
        return None
    major, separator, minor = version.partition(".")
    if separator != "." or not major.isdigit() or not minor.isdigit():
        return None
    return int(major)


def read_manifest(package: Path, *, require_publication: bool = False) -> dict[str, Any]:
    manifest_path = package / "manifest.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProductionPackageError(f"Cannot read production manifest: {manifest_path}") from error
    if not isinstance(value, dict):
        raise ProductionPackageError(f"Production manifest is not an object: {manifest_path}")
    manifest = cast(dict[str, Any], value)
    if (
        manifest.get("bundle_format") != "jrutil-production"
        or manifest.get("bundle_version") != 3
        or serving_schema_major(manifest.get("serving_schema_version")) != SERVING_SCHEMA_MAJOR
        or manifest.get("contract_valid") is not True
    ):
        raise ProductionPackageError("Unsupported or contract-invalid JrUtil production package")
    if require_publication and manifest.get("publication_eligible") is not True:
        raise ProductionPackageError("JrUtil production package is not publication eligible")
    for required in ("gtfs.zip", "diagnostics.json"):
        if not (package / required).is_file():
            raise ProductionPackageError(f"Production package is missing {required}")
    if not (package / "serving").is_dir():
        raise ProductionPackageError("Production package lacks serving/")
    return manifest


@contextmanager
def extracted_gtfs(package: Path) -> Generator[Path]:
    """Extract the validated flat GTFS projection for domain-specific checks."""

    archive_path = package / "gtfs.zip"
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if not names or any(
                not name or name.endswith("/") or Path(name).name != name or "\\" in name
                for name in names
            ):
                raise ProductionPackageError("Production gtfs.zip must contain flat files")
            with TemporaryDirectory(prefix="obehy-gtfs-") as temporary:
                root = Path(temporary)
                archive.extractall(root)
                yield root
    except zipfile.BadZipFile as error:
        raise ProductionPackageError(
            f"Malformed production GTFS archive: {archive_path}"
        ) from error


def package_digest(package: Path) -> str:
    """Hash a package tree independently of its absolute location."""

    digest = hashlib.sha256()
    for path in sorted(value for value in package.rglob("*") if value.is_file()):
        relative = path.relative_to(package).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(file_digest(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
