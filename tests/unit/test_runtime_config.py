from __future__ import annotations

from pathlib import Path

import pytest

from obehy.runtime_config import ConfigurationError, load_runtime_config


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_directory_runtime_with_absolute_paths(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "obehy.toml",
        f"""
schema_version = 1
[paths]
workdir = "{(tmp_path / "work").as_posix()}"
artifact_root = "{(tmp_path / "artifacts").as_posix()}"
routing_cache_dir = "{(tmp_path / "routing-cache").as_posix()}"
czptt_source_cache_dir = "{(tmp_path / "czptt-cache").as_posix()}"
osm_file = "{(tmp_path / "osm" / "region.osm.pbf").as_posix()}"
jrunify_ext_geodata_dir = "{(tmp_path / "geodata").as_posix()}"
gazetteer_dir = "{(tmp_path / "gazetteer").as_posix()}"
[jrutil]
directory = "{(tmp_path / "jrutil").as_posix()}"
""",
    )

    loaded = load_runtime_config(config)

    assert loaded.source == config.resolve()
    assert loaded.artifact_root == tmp_path / "artifacts"
    assert loaded.routing_cache_dir == tmp_path / "routing-cache"
    assert loaded.czptt_source_cache_dir == tmp_path / "czptt-cache"
    assert loaded.jrutil.directory == tmp_path / "jrutil"
    assert loaded.jrutil.command is None
    assert loaded.gazetteer_dir == tmp_path / "gazetteer"


def test_loads_command_runtime(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "obehy.toml",
        f"""
schema_version = 1
[paths]
workdir = "{(tmp_path / "work").as_posix()}"
osm_file = "{(tmp_path / "region.osm.pbf").as_posix()}"
jrunify_ext_geodata_dir = "{(tmp_path / "geodata").as_posix()}"
[jrutil]
command = ["{(tmp_path / "jrutil").as_posix()}"]
""",
    )

    loaded = load_runtime_config(config)

    assert loaded.artifact_root == tmp_path / "work"
    assert loaded.routing_cache_dir == tmp_path / "work" / "cache" / "routing"
    assert loaded.czptt_source_cache_dir == tmp_path / "work" / "cache" / "czptt-sources"
    assert loaded.jrutil.directory is None
    assert loaded.jrutil.command == ((tmp_path / "jrutil").as_posix(),)
    assert loaded.gazetteer_dir == tmp_path / "geodata" / "gazetteer"


@pytest.mark.parametrize(
    "change,match",
    [
        ("schema_version = 2", "schema_version"),
        ('workdir = "relative"', "absolute path"),
        (
            f'directory = "{Path("C:/jrutil").as_posix()}"\ncommand = ["dotnet"]',
            "exactly one",
        ),
    ],
)
def test_invalid_configuration_fails_immediately(tmp_path: Path, change: str, match: str) -> None:
    body = f"""
schema_version = 1
[paths]
workdir = "{(tmp_path / "work").as_posix()}"
osm_file = "{(tmp_path / "region.osm.pbf").as_posix()}"
jrunify_ext_geodata_dir = "{(tmp_path / "geodata").as_posix()}"
gazetteer_dir = "{(tmp_path / "gazetteer").as_posix()}"
[jrutil]
directory = "{(tmp_path / "jrutil").as_posix()}"
"""
    if change.startswith("schema_version"):
        body = body.replace("schema_version = 1", change)
    elif change.startswith("workdir"):
        body = body.replace(f'workdir = "{(tmp_path / "work").as_posix()}"', change)
    else:
        body = body.replace(f'directory = "{(tmp_path / "jrutil").as_posix()}"', change)

    with pytest.raises(ConfigurationError, match=match):
        load_runtime_config(_write(tmp_path / "invalid.toml", body))


def test_missing_default_has_setup_guidance(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"obehy\.example\.toml"):
        load_runtime_config(tmp_path / "missing.toml")
