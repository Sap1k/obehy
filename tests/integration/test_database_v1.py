from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from obehy.persistence.builds import BuildService
from obehy.persistence.jobs import BuildJobService
from obehy.persistence.models import (
    BuildJobEventRow,
    BuildSpecRow,
    LocationRow,
    StaticBuildRow,
    TripCallRow,
)
from obehy.persistence.resolver import StaticMappingResolver
from obehy.serving import ServingPackageError, ServingPackageLoader, validate_serving_package

from ..serving_fixture import fixture_rows, write_serving_package

pytestmark = pytest.mark.integration


def _create_build(
    session: Session,
    root: Path,
    rows: dict[str, list[dict[str, Any]]] | None = None,
) -> int:
    manifest = write_serving_package(root, rows)
    package = validate_serving_package(root)
    session.add(
        BuildSpecRow(
            sha256=manifest["build_spec_sha256"],
            schema_version=1,
            document={"schema_version": 1, "fixture": True},
        )
    )
    session.flush()
    return BuildService(session).create(
        feed_version=manifest["feed_version"],
        identity_contract=manifest["identity_contract"],
        build_spec_sha256=manifest["build_spec_sha256"],
        build_key_sha256="4" * 64,
        manifest_sha256=package.manifest_sha256,
        source_set_sha256=manifest["source_set_sha256"],
        overlay_policy_sha256=manifest["overlay_policy_sha256"],
        compiler_sha256=manifest["compiler_sha256"],
        compiler_identity=manifest["compiler_identity"],
        compiler_options_sha256=manifest["compiler_options_sha256"],
        registry_snapshot_sha256=None,
        gtfs_sha256=manifest["gtfs_sha256"],
        serving_sha256=manifest["serving_sha256"],
        netex_mapping_version=manifest["netex_mapping_version"],
        netex_target_schema=manifest["netex_target_schema"],
        netex_extension_version=manifest["netex_extension_version"],
        netex_mapping_sha256=manifest["netex_mapping_sha256"],
    )


def test_load_activate_and_resolve_serving_fixture(db_session: Session, tmp_path: Path) -> None:
    build_id = _create_build(db_session, tmp_path)
    package = validate_serving_package(tmp_path)
    counts = ServingPackageLoader(db_session).load(build_id, package)
    assert counts["trip_call"] == 2
    assert (
        db_session.scalar(
            select(func.count()).select_from(LocationRow).where(LocationRow.build_id == build_id)
        )
        == 3
    )
    assert (
        db_session.scalar(
            select(func.count()).select_from(TripCallRow).where(TripCallRow.build_id == build_id)
        )
        == 2
    )
    BuildService(db_session).activate(build_id)
    assert BuildService(db_session).active_build_id() == build_id
    resolver = StaticMappingResolver(db_session)
    assert resolver.source_trip(
        "pid-gtfs",
        "gtfs_trip_id",
        "pid-trip-1",
        date(2026, 6, 1),
        scheduled_start=3660,
        source_route_id="L1",
        source_start_location_id="U1Z1P",
    )
    assert resolver.source_entity("pid-gtfs", "gtfs_stop_id", "boarding_point", "U1Z1P")
    assert resolver.source_call(
        "pid-gtfs",
        "gtfs_trip_id",
        "pid-trip-1",
        "gtfs_stop_sequence",
        "1",
        date(2026, 6, 1),
        scheduled_start=3660,
    )
    assert (
        resolver.source_trip(
            "pid-gtfs",
            "vendor_trip_id",
            "pid-trip-1",
            date(2026, 6, 1),
            scheduled_start=3660,
        )
        is None
    )
    assert (
        resolver.source_trip(
            "pid-gtfs",
            "gtfs_trip_id",
            "pid-trip-1",
            date(2026, 6, 1),
            scheduled_start=3660,
            source_route_id="wrong-route",
        )
        is None
    )
    assert resolver.apply_alias("duk", "cis_line", "582588", date(2026, 6, 1)) == "001588"
    assert resolver.road_trip("001588", 1, date(2026, 6, 1))
    assert resolver.rail_trip(123, date(2026, 6, 1))
    row = db_session.get(StaticBuildRow, build_id)
    assert row is not None and row.state == "active" and row.partitions_attached


def test_job_queue_records_attempts_progress_and_retry(db_session: Session) -> None:
    db_session.add(
        BuildSpecRow(
            sha256="9" * 64,
            schema_version=1,
            document={"schema_version": 1},
        )
    )
    db_session.flush()
    service = BuildJobService(db_session)
    job_id = service.enqueue("9" * 64, priority=10)
    assert service.claim("worker-1") == (job_id, 1)
    assert not service.heartbeat(job_id, 1, {"phase": "compile", "percent": 50})
    service.finish(job_id, 1, succeeded=False, exit_code=1, error={"message": "failed"})
    service.retry(job_id)
    assert service.claim("worker-2") == (job_id, 2)
    service.finish(job_id, 2, succeeded=True, exit_code=0)
    event_count = db_session.scalar(
        select(func.count()).select_from(BuildJobEventRow).where(BuildJobEventRow.job_id == job_id)
    )
    assert event_count == 7


def test_invalid_setwise_payload_leaves_no_partition_and_marks_build_failed(
    db_session: Session, tmp_path: Path
) -> None:
    rows = fixture_rows()
    rows["trip_call"][0]["boarding_point_id"] = None
    build_id = _create_build(db_session, tmp_path, rows)
    package = validate_serving_package(tmp_path)
    with pytest.raises(ServingPackageError, match="trip call shape"):
        ServingPackageLoader(db_session).load(build_id, package)
    row = db_session.get(StaticBuildRow, build_id)
    assert row is not None and row.state == "failed" and not row.partitions_attached
    assert db_session.scalar(select(func.to_regclass(f"static.trip_call_b{build_id}"))) is None


def test_activation_rollback_and_retention_are_one_build_pointer(db_session: Session) -> None:
    spec_digest = "7" * 64
    db_session.add(
        BuildSpecRow(
            sha256=spec_digest,
            schema_version=1,
            document={"schema_version": 1, "retention_fixture": True},
        )
    )
    builds: list[StaticBuildRow] = []
    for number in range(4):
        marker = format(number + 10, "064x")
        row = StaticBuildRow(
            feed_version=f"retention-{number}",
            state="ready",
            identity_contract="provisional-v0",
            build_spec_sha256=spec_digest,
            build_key_sha256=marker,
            manifest_sha256=marker,
            source_set_sha256=marker,
            overlay_policy_sha256=marker,
            compiler_sha256=marker,
            compiler_identity={"fixture": number},
            compiler_options_sha256=marker,
            registry_snapshot_sha256=None,
            gtfs_sha256=marker,
            serving_sha256=marker,
            netex_mapping_version="1",
            netex_target_schema="v2.0.0",
            netex_extension_version="1",
            netex_mapping_sha256=marker,
            partitions_attached=True,
        )
        db_session.add(row)
        db_session.flush()
        builds.append(row)
        BuildService(db_session).activate(row.id)
    lifecycle = BuildService(db_session)
    lifecycle.activate(builds[2].id)
    assert lifecycle.active_build_id() == builds[2].id
    lifecycle.activate(builds[3].id)
    pruned = lifecycle.prune_after_activation(retain=3)
    assert pruned == (builds[0].id,)
    assert builds[0].state == "pruned"
    assert builds[3].state == "active"


def test_switching_one_publication_preserves_a_shared_build(db_session: Session) -> None:
    spec_digest = "6" * 64
    db_session.add(
        BuildSpecRow(
            sha256=spec_digest,
            schema_version=1,
            document={"schema_version": 1, "shared_publication_fixture": True},
        )
    )
    db_session.flush()
    builds: list[StaticBuildRow] = []
    for number in range(2):
        marker = format(number + 20, "064x")
        row = StaticBuildRow(
            feed_version=f"shared-publication-{number}",
            state="ready",
            identity_contract="provisional-v0",
            build_spec_sha256=spec_digest,
            build_key_sha256=marker,
            manifest_sha256=marker,
            source_set_sha256=marker,
            overlay_policy_sha256=marker,
            compiler_sha256=marker,
            compiler_identity={"fixture": number},
            compiler_options_sha256=marker,
            registry_snapshot_sha256=None,
            gtfs_sha256=marker,
            serving_sha256=marker,
            netex_mapping_version="1",
            netex_target_schema="v2.0.0",
            netex_extension_version="1",
            netex_mapping_sha256=marker,
            partitions_attached=True,
        )
        db_session.add(row)
        db_session.flush()
        builds.append(row)

    lifecycle = BuildService(db_session)
    lifecycle.activate(builds[0].id, publication="public")
    lifecycle.activate(builds[0].id, publication="preview")
    lifecycle.activate(builds[1].id, publication="public")

    assert lifecycle.active_build_id("preview") == builds[0].id
    assert builds[0].state == "active"
    assert builds[0].id not in lifecycle.prune_after_activation(retain=1)

    lifecycle.activate(builds[1].id, publication="preview")
    assert builds[0].state == "retired"
