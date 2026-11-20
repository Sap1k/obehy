"""How names and codes read (`api/labels.py`). Ported from the web's tests when the rules moved
here."""

from __future__ import annotations

from obehy.api import schema
from obehy.api.labels import colour, plate, vehicle_name


def test_czech_plates_read_as_they_are_written() -> None:
    assert [plate(p) for p in ("1TD8408", "5L05324", "EL928FK", "BM123AB")] == [
        "1TD 8408",
        "5L0 5324",
        "EL 928FK",
        "BM123AB",
    ]
    traits = schema.VehicleTraits(licence_plate="2UC6580")
    assert traits.licence_plate == "2UC 6580"


def test_a_colour_reads_as_css_and_android_read_it() -> None:
    assert (colour("7A0603"), colour("#7a0603"), colour(""), colour(None)) == (
        "#7A0603",
        "#7a0603",
        None,
        None,
    )
    line = schema.Line(
        route_id="r", short_name="1", long_name=None, mode="bus", color="FFFFFF", text_color=None
    )
    assert (line.color, line.text_color) == ("#FFFFFF", None)


def test_a_vehicle_reads_by_its_fleet_number_else_label_else_source_id() -> None:
    assert vehicle_name("1001", "A", "x") == "1001"
    assert vehicle_name(None, "A", "x") == "A"
    assert vehicle_name(None, None, "x") == "x"
