from pathlib import Path

from obehy.fixtures.projections import load_projection

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_all_expected_projections_are_utf8_json_objects() -> None:
    paths = sorted(FIXTURES.glob("**/expected.json"))
    assert len(paths) == 5
    projections = [load_projection(path) for path in paths]
    assert all("format" in projection for projection in projections)
    assert "Jedlová" in (FIXTURES / "pid/native/stops.txt").read_text(encoding="utf-8")


def test_jdf_continuity_fixture_documents_mock_authority() -> None:
    export_a = load_projection(FIXTURES / "jdf/export_a/expected.json")
    export_b = load_projection(FIXTURES / "jdf/export_b/expected.json")
    stops_a = export_a["stops"]
    stops_b = export_b["stops"]
    assert isinstance(stops_a, list) and isinstance(stops_b, list)
    assert stops_a[0]["export_stop_id"] != stops_b[0]["export_stop_id"]
    assert stops_a[0]["authoritative_cis_stop_id"] == stops_b[0]["authoritative_cis_stop_id"]
