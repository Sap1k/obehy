import pytest

from obehy.domain.identifiers import CisLineId, CisTripId, PublicId, TrainNumber


@pytest.mark.parametrize(
    "value",
    ["v0:trip:0123456789abcdef", "rail:CZ:12345:subsidiary-with-no-length-limit"],
)
def test_public_ids_are_opaque_unrestricted_text(value: str) -> None:
    assert str(PublicId(value)) == value


@pytest.mark.parametrize("value", ["", "bad\x00id"])
def test_public_ids_reject_only_values_postgresql_cannot_store(value: str) -> None:
    with pytest.raises(ValueError):
        PublicId(value)


def test_external_identifiers_remain_typed() -> None:
    assert str(CisLineId("001588")) == "001588"
    assert CisTripId(0).value == 0
    assert TrainNumber(123).value == 123


def test_invalid_external_identifiers_are_rejected() -> None:
    with pytest.raises(ValueError):
        CisLineId("12")
    with pytest.raises(ValueError):
        CisTripId(-1)
    with pytest.raises(ValueError):
        TrainNumber(0)
