from obehy.persistence.models import Base


def test_database_v1_has_explicit_control_and_static_boundaries() -> None:
    tables = set(Base.metadata.tables)
    assert "control.static_build" in tables
    assert "control.build_job" in tables
    assert "static.trip_call" in tables
    assert "static.source_trip_map" in tables
    assert "static.service_feature_assignment" in tables
    assert "static.location_feature" in tables
    assert "static.connection_claim" in tables
    assert not {
        "canonical_entity",
        "source_object",
        "source_schedule_call",
        "identity_diagnostic",
        "source_binding",
    }.intersection(tables)


def test_all_serving_tables_are_list_partitioned_by_build() -> None:
    static_tables = [table for table in Base.metadata.tables.values() if table.schema == "static"]
    assert static_tables
    assert all(
        table.dialect_options["postgresql"]["partition_by"] == "LIST (build_id)"
        for table in static_tables
    )
    assert all("build_id" in table.primary_key.columns for table in static_tables)
