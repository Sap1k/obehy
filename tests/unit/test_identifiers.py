import pytest

from obehy.domain.identifiers import CanonicalId, CisLineId, CisTripId, EntityKind, TrainNumber


def test_canonical_ids_are_typed_and_fixed_width() -> None:
    canonical_id = CanonicalId.from_number(EntityKind.STOP_PLACE, 42)
    assert str(canonical_id) == "S000000042"
    assert canonical_id.kind is EntityKind.STOP_PLACE


@pytest.mark.parametrize("value", ["S0", "X000000001", "S000000000", "S0000000001"])
def test_invalid_canonical_id_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        CanonicalId(value)


def test_czech_identifiers_validate_without_lossy_coercion() -> None:
    assert CisLineId("001588").value == "001588"
    assert CisTripId(0).value == 0
    assert TrainNumber(9001).value == 9001
    with pytest.raises(ValueError):
        CisLineId("1588")
    with pytest.raises(ValueError):
        CisTripId(-1)
    with pytest.raises(ValueError):
        TrainNumber(0)
