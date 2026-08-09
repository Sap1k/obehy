from pathlib import Path

import pytest

from obehy import national_jdf
from obehy.pipeline_support import atomic_output_path, file_digest, write_json


def test_atomic_output_activates_and_cleans_temporary_path(tmp_path: Path) -> None:
    destination = tmp_path / "result.txt"
    with atomic_output_path(destination) as temporary:
        temporary.write_bytes(b"complete\n")
        assert not destination.exists()
    assert destination.read_bytes() == b"complete\n"
    assert list(tmp_path.glob("*.part")) == []


def test_atomic_output_preserves_destination_on_failure(tmp_path: Path) -> None:
    destination = tmp_path / "result.txt"
    destination.write_bytes(b"old\n")
    with (
        pytest.raises(RuntimeError, match="stop"),
        atomic_output_path(destination) as temporary,
    ):
        temporary.write_bytes(b"partial\n")
        raise RuntimeError("stop")
    assert destination.read_bytes() == b"old\n"
    assert list(tmp_path.glob("*.part")) == []


def test_deterministic_helpers_are_reexported_for_compatibility(tmp_path: Path) -> None:
    destination = tmp_path / "value.json"
    write_json(destination, {"ž": 1, "a": 2})
    assert destination.read_bytes() == b'{\n  "a": 2,\n  "\xc5\xbe": 1\n}\n'
    assert national_jdf.file_digest(destination) == file_digest(destination)
    assert national_jdf.write_json is write_json
