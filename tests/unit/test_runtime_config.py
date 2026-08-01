from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

import pytest

from obehy import national_czptt, national_jdf
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
osm_file = "{(tmp_path / "osm" / "region.osm.pbf").as_posix()}"
jrunify_ext_geodata_dir = "{(tmp_path / "geodata").as_posix()}"
[jrutil]
directory = "{(tmp_path / "jrutil").as_posix()}"
""",
    )

    loaded = load_runtime_config(config)

    assert loaded.source == config.resolve()
    assert loaded.jrutil.directory == tmp_path / "jrutil"
    assert loaded.jrutil.command is None


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
command = ["dotnet", "{(tmp_path / "jrutil.dll").as_posix()}"]
""",
    )

    loaded = load_runtime_config(config)

    assert loaded.jrutil.directory is None
    assert loaded.jrutil.command == ("dotnet", (tmp_path / "jrutil.dll").as_posix())


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


@pytest.mark.parametrize(
    "parser,obsolete",
    [
        (national_jdf._parser, "--jrutil-root"),  # pyright: ignore[reportPrivateUsage]
        (national_jdf._parser, "--geodata-root"),  # pyright: ignore[reportPrivateUsage]
        (national_czptt._parser, "--jrutil-root"),  # pyright: ignore[reportPrivateUsage]
    ],
)
def test_national_clis_have_no_parent_fallback_flags(
    parser: Callable[[], argparse.ArgumentParser], obsolete: str, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(["build", "--output", str(tmp_path / "out"), obsolete, "x"])
