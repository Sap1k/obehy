import pytest

from obehy.persistence.control import ControlDataError, document_sha256, relative_storage_key


def test_build_documents_hash_canonical_json() -> None:
    assert document_sha256({"b": 2, "a": 1}) == document_sha256({"a": 1, "b": 2})


def test_storage_keys_are_relative_and_canonical() -> None:
    assert relative_storage_key("raw/pid/abc/feed.zip") == "raw/pid/abc/feed.zip"
    for invalid in ("/absolute", "C:/absolute", "raw\\windows", "raw/../escape"):
        with pytest.raises(ControlDataError):
            relative_storage_key(invalid)
