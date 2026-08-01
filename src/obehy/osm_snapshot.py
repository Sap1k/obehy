"""Build and validate the shared regional OpenStreetMap snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.request import Request, urlopen

from obehy.runtime_config import ConfigurationError, RuntimeConfig, load_runtime_config

GEOFABRIK_BASE = "https://download.geofabrik.de/europe"
MERGE_SCHEMA_VERSION = 2
REGIONS = (
    "czech-republic",
    "austria",
    "germany/bayern",
    "germany/sachsen",
    "slovakia",
    "poland/dolnoslaskie",
    "poland/opolskie",
    "poland/slaskie",
)
ODBL_ATTRIBUTION = (
    "© OpenStreetMap contributors; data available under the Open Database License (ODbL)"
)


class OsmSnapshotError(RuntimeError):
    """The shared OSM snapshot could not be built or validated."""


class _Response(Protocol):
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


@dataclass(frozen=True)
class Extract:
    region_id: str
    url: str
    remote_md5: str
    sha256: str
    bytes: int
    path: str
    retrieved_at: str


@dataclass(frozen=True)
class OsmiumRuntime:
    command: tuple[str, ...]
    path_style: str
    identity: str


FetchBytes = Callable[[str], bytes]
MergeFn = Callable[[Sequence[Path], Path], dict[str, object]]
IdentityFn = Callable[[], str]


class RailwayFilterFn(Protocol):
    def __call__(
        self,
        source: Path,
        destination: Path,
        *,
        source_key: str | None = None,
    ) -> Path: ...


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _progress(message: str) -> None:
    print(f"[obehy-osm] {message}", file=sys.stderr, flush=True)


def _format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GiB"
    return f"{value / 1024**2:.1f} MiB"


def file_digest(
    path: Path,
    algorithm: str = "sha256",
    *,
    progress_label: str | None = None,
) -> str:
    digest = hashlib.new(algorithm)
    size = path.stat().st_size
    completed = 0
    started = time.monotonic()
    last_report = started
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            completed += len(chunk)
            now = time.monotonic()
            if progress_label is not None and now - last_report >= 1:
                percent = completed * 100 / size if size else 100
                rate = completed / max(now - started, 0.001)
                _progress(
                    f"{progress_label}: {percent:5.1f}% "
                    f"({_format_bytes(completed)}, {_format_bytes(int(rate))}/s)"
                )
                last_report = now
    if progress_label is not None:
        elapsed = max(time.monotonic() - started, 0.001)
        _progress(f"{progress_label}: complete ({_format_bytes(completed)} in {elapsed:.1f}s)")
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Obehy/0.1 OSM snapshot builder"})
    with cast(_Response, urlopen(request, timeout=120)) as response:
        return response.read()


def _md5(payload: bytes, url: str) -> str:
    match = re.search(rb"(?i)\b([0-9a-f]{32})\b", payload)
    if match is None:
        raise OsmSnapshotError(f"Geofabrik sidecar contains no MD5 digest: {url}")
    return match.group(1).decode("ascii").lower()


def _url(region_id: str) -> str:
    return f"{GEOFABRIK_BASE}/{region_id}-latest.osm.pbf"


def _safe_region(region_id: str) -> str:
    return region_id.replace("/", "--")


def _download_extract(
    region_id: str,
    cache_root: Path,
    fetch: FetchBytes,
) -> Extract:
    url = _url(region_id)
    md5_url = url + ".md5"
    _progress(f"{region_id}: checking Geofabrik edition")
    remote_md5 = _md5(fetch(md5_url), md5_url)
    region_root = cache_root / _safe_region(region_id)
    destination = region_root / f"{remote_md5}.osm.pbf"
    metadata_path = destination.with_suffix(destination.suffix + ".json")
    if destination.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        stats = destination.stat()
        recorded_stats = cast(dict[str, object], metadata.get("local_file_stats", {}))
        if metadata.get("remote_md5") == remote_md5 and metadata.get("bytes") == stats.st_size:
            record = Extract(
                region_id=region_id,
                url=url,
                remote_md5=remote_md5,
                sha256=str(metadata["sha256"]),
                bytes=int(metadata["bytes"]),
                path=str(destination.resolve()),
                retrieved_at=str(metadata["retrieved_at"]),
            )
            if (
                recorded_stats.get("bytes") == stats.st_size
                and recorded_stats.get("mtime_ns") == stats.st_mtime_ns
            ):
                _progress(f"{region_id}: cached {_format_bytes(record.bytes)} ({remote_md5})")
                return record
            if (
                file_digest(destination, "md5") == remote_md5
                and file_digest(destination) == record.sha256
            ):
                write_json(
                    metadata_path,
                    {
                        **asdict(record),
                        "local_file_stats": {
                            "bytes": stats.st_size,
                            "mtime_ns": stats.st_mtime_ns,
                        },
                    },
                )
                _progress(
                    f"{region_id}: cached {_format_bytes(record.bytes)} verified ({remote_md5})"
                )
                return record

    region_root.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    digest_md5 = hashlib.md5()
    digest_sha256 = hashlib.sha256()
    size = 0
    started = time.monotonic()
    last_report = started
    request = Request(url, headers={"User-Agent": "Obehy/0.1 OSM snapshot builder"})
    _progress(f"{region_id}: downloading {url}")
    try:
        with (
            cast(_Response, urlopen(request, timeout=120)) as response,
            temporary.open("wb") as output,
        ):
            headers = getattr(response, "headers", {})
            raw_total = headers.get("Content-Length") if hasattr(headers, "get") else None
            total = int(raw_total) if raw_total and str(raw_total).isdigit() else None
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest_md5.update(chunk)
                digest_sha256.update(chunk)
                size += len(chunk)
                now = time.monotonic()
                if now - last_report >= 1:
                    rate = size / max(now - started, 0.001)
                    if total:
                        status = f"{size * 100 / total:5.1f}% ({_format_bytes(size)})"
                    else:
                        status = _format_bytes(size)
                    _progress(f"{region_id}: {status}, {_format_bytes(int(rate))}/s")
                    last_report = now
        if digest_md5.hexdigest() != remote_md5:
            raise OsmSnapshotError(
                f"MD5 mismatch for {region_id}: expected {remote_md5}, "
                f"received {digest_md5.hexdigest()}"
            )
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    elapsed = max(time.monotonic() - started, 0.001)
    _progress(
        f"{region_id}: downloaded {_format_bytes(size)} in {elapsed:.1f}s "
        f"({_format_bytes(int(size / elapsed))}/s)"
    )

    record = Extract(
        region_id=region_id,
        url=url,
        remote_md5=remote_md5,
        sha256=digest_sha256.hexdigest(),
        bytes=size,
        path=str(destination.resolve()),
        retrieved_at=utc_now(),
    )
    stats = destination.stat()
    write_json(
        metadata_path,
        {
            **asdict(record),
            "local_file_stats": {
                "bytes": stats.st_size,
                "mtime_ns": stats.st_mtime_ns,
            },
        },
    )
    return record


def _discover_osmium() -> OsmiumRuntime:
    candidates: list[tuple[tuple[str, ...], str]] = []
    native = shutil.which("osmium")
    if native is not None:
        candidates.append(((native,), "native"))
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl is not None:
            candidates.append(((wsl, "--exec", "osmium"), "wsl"))

    failures: list[str] = []
    for command, path_style in candidates:
        result = subprocess.run(
            [*command, "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            lines = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith(("osmium version", "libosmium version"))
            ]
            identity = "; ".join(lines) or result.stdout.strip().splitlines()[0]
            return OsmiumRuntime(command, path_style, identity)
        failures.append(f"{' '.join(command)}: exit {result.returncode}")

    detail = f" Probes: {'; '.join(failures)}." if failures else ""
    raise OsmSnapshotError(
        "The native Osmium tool is required to merge OSM snapshots. Install `osmium-tool` "
        "and make `osmium` available on PATH; on Windows, installing it in the default WSL "
        f"distribution is also supported.{detail}"
    )


def _osmium_path(runtime: OsmiumRuntime, path: Path) -> str:
    resolved = path.resolve()
    if runtime.path_style == "native":
        return str(resolved)
    if runtime.path_style != "wsl":
        raise OsmSnapshotError(f"Unsupported Osmium path style: {runtime.path_style}")
    drive = resolved.drive
    if len(drive) != 2 or drive[1] != ":":
        raise OsmSnapshotError(
            f"WSL Osmium requires a drive-letter path under the default /mnt mount: {resolved}"
        )
    windows_path = resolved.as_posix()
    return f"/mnt/{drive[0].lower()}/{windows_path[3:]}"


def _run_osmium(
    runtime: OsmiumRuntime,
    arguments: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    command = [*runtime.command, *arguments]
    result = subprocess.run(
        command,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise OsmSnapshotError(
            f"Osmium failed with exit code {result.returncode}: {' '.join(command)}"
        )
    return result


def _merge_with_osmium(
    inputs: Sequence[Path],
    destination: Path,
    runtime: OsmiumRuntime | None = None,
) -> dict[str, object]:
    runtime = runtime or _discover_osmium()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part.osm.pbf")
    if temporary.exists():
        temporary.unlink()
    _progress(
        f"native merge starting: {len(inputs)} extracts, "
        f"{_format_bytes(sum(path.stat().st_size for path in inputs))}"
    )
    _progress(f"Osmium runtime: {runtime.identity}")
    started = time.monotonic()
    try:
        _run_osmium(
            runtime,
            [
                "merge",
                "--progress",
                "--verbose",
                "--overwrite",
                "--generator=Obehy shared OSM merger/2",
                "--output-format=pbf,pbf_compression=zlib",
                "--output-header=sorting=Type_then_ID",
                "--output",
                _osmium_path(runtime, temporary),
                *(_osmium_path(runtime, path) for path in inputs),
            ],
        )
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise

    elapsed = max(time.monotonic() - started, 0.001)
    _progress(
        f"native merge complete: {_format_bytes(destination.stat().st_size)} in {elapsed:.1f}s"
    )
    return {
        "engine": "osmium-tool",
        "osmium": runtime.identity,
        "command": list(runtime.command),
        "generator": "Obehy shared OSM merger/2",
        "compression": "PBF zlib",
    }


def filter_railway_locations(
    source: Path,
    destination: Path,
    *,
    source_key: str | None = None,
) -> Path:
    """Extract tagged railway-location nodes with native Osmium."""
    source = source.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = active_manifest_path(destination)
    if source_key is not None and destination.is_file() and manifest_path.is_file():
        try:
            manifest = cast(
                dict[str, Any],
                json.loads(manifest_path.read_text(encoding="utf-8")),
            )
            stats = destination.stat()
            output = cast(dict[str, object], manifest.get("output", {}))
            if (
                manifest.get("schema_version") == 1
                and manifest.get("filter_schema") == "railway-location-nodes-v2"
                and manifest.get("source_key") == source_key
                and output.get("bytes") == stats.st_size
                and output.get("mtime_ns") == stats.st_mtime_ns
            ):
                _progress(
                    f"railway-location filter reused: {_format_bytes(stats.st_size)}; "
                    f"source key={source_key[:24]}"
                )
                return destination
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    runtime = _discover_osmium()
    temporary = destination.with_name(f"{destination.stem}.{uuid.uuid4().hex}.part.osm.pbf")
    _progress(f"railway-location filter starting: {_format_bytes(source.stat().st_size)}")
    started = time.monotonic()
    try:
        _run_osmium(
            runtime,
            [
                "tags-filter",
                "--progress",
                "--verbose",
                "--overwrite",
                "--omit-referenced",
                "--generator=Obehy CZPTT railway-location filter/2",
                "--output-format=pbf,pbf_compression=zlib",
                "--output",
                _osmium_path(runtime, temporary),
                _osmium_path(runtime, source),
                "n/railway=station,halt,stop",
            ],
        )
        os.replace(temporary, destination)
        stats = destination.stat()
        write_json(
            manifest_path,
            {
                "schema_version": 1,
                "filter_schema": "railway-location-nodes-v2",
                "source_key": source_key,
                "source_file": str(source),
                "created_at": utc_now(),
                "output": {
                    "file": str(destination),
                    "bytes": stats.st_size,
                    "mtime_ns": stats.st_mtime_ns,
                    "sha256": file_digest(destination),
                },
                "osmium": runtime.identity,
            },
        )
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    elapsed = max(time.monotonic() - started, 0.001)
    _progress(
        f"railway-location filter complete: {_format_bytes(destination.stat().st_size)} "
        f"in {elapsed:.1f}s"
    )
    return destination


def railway_locations_path(workdir: Path) -> Path:
    return workdir.resolve() / "osm" / "railway-locations.osm.pbf"


def validate_railway_locations(workdir: Path, source_key: str) -> Path:
    destination = railway_locations_path(workdir)
    manifest_path = active_manifest_path(destination)
    guidance = "Run `obehy-osm build` with the same configuration."
    if not destination.is_file() or not manifest_path.is_file():
        raise OsmSnapshotError(f"Railway-location OSM extract is missing. {guidance}")
    try:
        manifest = cast(
            dict[str, Any],
            json.loads(manifest_path.read_text(encoding="utf-8")),
        )
        output = cast(dict[str, object], manifest.get("output", {}))
        stats = destination.stat()
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        raise OsmSnapshotError(f"Railway-location OSM manifest is invalid. {guidance}") from error
    if (
        manifest.get("schema_version") != 1
        or manifest.get("filter_schema") != "railway-location-nodes-v2"
        or manifest.get("source_key") != source_key
        or output.get("bytes") != stats.st_size
        or output.get("mtime_ns") != stats.st_mtime_ns
    ):
        raise OsmSnapshotError(
            f"Railway-location OSM extract does not match the active snapshot. {guidance}"
        )
    return destination


def _merge_key(extracts: Sequence[Extract], osmium_identity: str) -> str:
    payload = {
        "merge_schema_version": MERGE_SCHEMA_VERSION,
        "osmium_identity": osmium_identity,
        "inputs": [{"region_id": value.region_id, "sha256": value.sha256} for value in extracts],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_snapshot(
    config: RuntimeConfig,
    *,
    verify: bool = False,
    fetch: FetchBytes = _fetch_bytes,
    merge: MergeFn = _merge_with_osmium,
    identity: IdentityFn | None = None,
    railway_filter: RailwayFilterFn = filter_railway_locations,
) -> Path:
    _progress(f"build started; workdir={config.workdir.resolve()}")
    workdir = config.workdir.resolve()
    extracts_root = workdir / "osm" / "extracts"
    extracts = [_download_extract(region, extracts_root, fetch) for region in REGIONS]
    runtime: OsmiumRuntime | None = None
    merge_action = merge
    if merge is _merge_with_osmium:
        runtime = _discover_osmium()

        def native_merge(inputs: Sequence[Path], output: Path) -> dict[str, object]:
            return _merge_with_osmium(inputs, output, runtime)

        merge_action = native_merge
    osmium_identity = (
        identity()
        if identity is not None
        else (runtime.identity if runtime is not None else "custom-merge")
    )

    if verify:
        _progress("full verification requested; hashing all cached extracts")
        for extract in extracts:
            path = Path(extract.path)
            if (
                file_digest(
                    path,
                    "md5",
                    progress_label=f"{extract.region_id}: MD5",
                )
                != extract.remote_md5
                or file_digest(
                    path,
                    progress_label=f"{extract.region_id}: SHA-256",
                )
                != extract.sha256
            ):
                raise OsmSnapshotError(
                    f"Cached OSM extract failed full verification: {extract.region_id}"
                )

    # A changed source during the build would make the manifest incoherent.
    _progress("rechecking Geofabrik editions before merge")
    after = [_md5(fetch(_url(region) + ".md5"), _url(region) + ".md5") for region in REGIONS]
    before = [value.remote_md5 for value in extracts]
    if after != before:
        raise OsmSnapshotError("A Geofabrik source rolled over during download; rerun the build")

    merge_key = _merge_key(extracts, osmium_identity)
    source_key = merge_key[:24]
    output_path = config.osm_file.resolve()
    manifest_path = active_manifest_path(output_path)
    existing_manifest: dict[str, Any] | None = None
    reuse = False
    if output_path.is_file() and manifest_path.is_file():
        try:
            existing_manifest = cast(
                dict[str, Any],
                json.loads(manifest_path.read_text(encoding="utf-8")),
            )
        except (json.JSONDecodeError, OSError):
            existing_manifest = None
        if (
            existing_manifest is not None
            and existing_manifest.get("schema_version") == 2
            and existing_manifest.get("merge_key") == merge_key
            and existing_manifest.get("regions") == list(REGIONS)
        ):
            expected = cast(dict[str, object], existing_manifest.get("output", {}))
            stats = output_path.stat()
            recorded_stats = cast(
                dict[str, object],
                existing_manifest.get("active_file_stats", {}),
            )
            unchanged_stats = (
                stats.st_size == expected.get("bytes")
                and recorded_stats.get("bytes") == stats.st_size
                and recorded_stats.get("mtime_ns") == stats.st_mtime_ns
            )
            reuse = unchanged_stats or (
                stats.st_size == expected.get("bytes")
                and file_digest(
                    output_path,
                    progress_label="active snapshot SHA-256",
                )
                == expected.get("sha256")
            )

    if not reuse:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        details = merge_action([Path(value.path) for value in extracts], output_path)
        output = {
            "bytes": output_path.stat().st_size,
            "sha256": file_digest(
                output_path,
                progress_label="merged snapshot SHA-256",
            ),
        }
        stats = output_path.stat()
        manifest: dict[str, object] = {
            "schema_version": 2,
            "merge_key": merge_key,
            "merge_schema_version": MERGE_SCHEMA_VERSION,
            "created_at": utc_now(),
            "regions": list(REGIONS),
            "sources": [asdict(value) for value in extracts],
            "osmium_identity": osmium_identity,
            "output": output,
            "merge": details,
            "attribution": ODBL_ATTRIBUTION,
            "active_file": str(output_path),
            "active_file_stats": {
                "bytes": stats.st_size,
                "mtime_ns": stats.st_mtime_ns,
            },
        }
        write_json(manifest_path, manifest)
        _progress(f"active OSM file regenerated; source key={source_key}")
    else:
        _progress(f"active OSM file reused; source key={source_key}")
        if verify and existing_manifest is not None:
            expected = cast(dict[str, object], existing_manifest["output"])
            if file_digest(
                output_path,
                progress_label="active snapshot SHA-256",
            ) != expected.get("sha256"):
                raise OsmSnapshotError("Active OSM snapshot failed full verification")

    railway_filter(
        output_path,
        railway_locations_path(workdir),
        source_key=merge_key,
    )
    _progress(f"build complete; source key={source_key}")
    return output_path


def active_manifest_path(osm_file: Path) -> Path:
    return osm_file.with_suffix(osm_file.suffix + ".manifest.json")


def validate_snapshot(osm_file: Path, workdir: Path, *, full_hash: bool = False) -> dict[str, Any]:
    del workdir
    manifest_path = active_manifest_path(osm_file)
    guidance = "Run `obehy-osm build` with the same configuration."
    if not osm_file.is_file() or not manifest_path.is_file():
        raise OsmSnapshotError(f"Configured OSM snapshot or manifest is missing. {guidance}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2 or not isinstance(manifest.get("merge_key"), str):
        raise OsmSnapshotError(f"Configured OSM manifest has an unsupported schema. {guidance}")
    expected_value = manifest.get("output")
    if not isinstance(expected_value, dict):
        raise OsmSnapshotError(f"Configured OSM manifest lacks output identity. {guidance}")
    expected = cast(dict[str, object], expected_value)
    stats = osm_file.stat()
    if stats.st_size != expected.get("bytes"):
        raise OsmSnapshotError(
            f"Configured OSM snapshot size differs from its manifest. {guidance}"
        )
    recorded_stats = cast(dict[str, object], manifest.get("active_file_stats", {}))
    changed_stats = (
        recorded_stats.get("bytes") != stats.st_size
        or recorded_stats.get("mtime_ns") != stats.st_mtime_ns
    )
    if (full_hash or changed_stats) and file_digest(osm_file) != expected.get("sha256"):
        raise OsmSnapshotError(
            f"Configured OSM snapshot hash differs from its manifest. {guidance}"
        )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obehy-osm")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build or reuse the shared regional OSM snapshot")
    build.add_argument("--config", type=Path)
    build.add_argument("--verify", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_runtime_config(cast(Path | None, args.config))
        result = build_snapshot(config, verify=cast(bool, args.verify))
    except (ConfigurationError, OSError, OsmSnapshotError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Shared OSM snapshot ready: {result}")
    return 0
