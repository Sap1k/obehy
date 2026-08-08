from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from obehy.persistence.models import (
    BuildDiagnosticRow,
    BuildValidationRow,
    PublicationRow,
    StaticBuildRow,
)

STATIC_RELATIONS = (
    "agency",
    "location",
    "route",
    "service_calendar",
    "service_exception",
    "shape",
    "shape_point",
    "trip",
    "trip_call",
    "route_segment",
    "transfer",
    "fare_system",
    "fare_zone",
    "location_zone",
    "call_zone",
    "service_note",
    "service_note_assignment",
    "service_feature_assignment",
    "location_feature",
    "connection_claim",
    "travel_restriction_assignment",
    "operational_location",
    "operational_journey",
    "operational_call",
    "source_entity_map",
    "source_trip_map",
    "source_call_map",
    "source_trip_coverage",
    "identifier_alias",
    "road_route_key",
    "road_trip_key",
    "rail_trip_key",
    "selected_field_provenance",
)


class BuildLifecycleError(RuntimeError):
    pass


class BuildService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        feed_version: str,
        identity_contract: str,
        build_spec_sha256: str,
        build_key_sha256: str,
        manifest_sha256: str,
        source_set_sha256: str,
        overlay_policy_sha256: str,
        compiler_sha256: str,
        compiler_identity: dict[str, Any],
        compiler_options_sha256: str,
        registry_snapshot_sha256: str | None,
        gtfs_sha256: str,
        serving_sha256: str,
        netex_mapping_version: str,
        netex_target_schema: str,
        netex_extension_version: str,
        netex_mapping_sha256: str,
    ) -> int:
        existing = self.session.scalar(
            select(StaticBuildRow.id).where(StaticBuildRow.build_key_sha256 == build_key_sha256)
        )
        if existing is not None:
            return existing
        row = StaticBuildRow(
            feed_version=feed_version,
            state="building",
            identity_contract=identity_contract,
            build_spec_sha256=build_spec_sha256,
            build_key_sha256=build_key_sha256,
            manifest_sha256=manifest_sha256,
            source_set_sha256=source_set_sha256,
            overlay_policy_sha256=overlay_policy_sha256,
            compiler_sha256=compiler_sha256,
            compiler_identity=compiler_identity,
            compiler_options_sha256=compiler_options_sha256,
            registry_snapshot_sha256=registry_snapshot_sha256,
            gtfs_sha256=gtfs_sha256,
            serving_sha256=serving_sha256,
            netex_mapping_version=netex_mapping_version,
            netex_target_schema=netex_target_schema,
            netex_extension_version=netex_extension_version,
            netex_mapping_sha256=netex_mapping_sha256,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def mark_loading(self, build_id: int) -> None:
        row = self._locked(build_id)
        if row.state != "building":
            raise BuildLifecycleError("Only a building build can start loading")
        row.state = "loading"
        self.session.flush()

    def mark_ready(self, build_id: int) -> None:
        row = self._locked(build_id)
        if row.state != "loading" or not row.partitions_attached:
            raise BuildLifecycleError("Only a completely attached loading build can become ready")
        if self._has_blocker(build_id):
            raise BuildLifecycleError("Build has blocking validation or diagnostics")
        row.state = "ready"
        row.ready_at = datetime.now(UTC)
        self.session.flush()

    def mark_failed(self, build_id: int, details: dict[str, Any]) -> None:
        row = self._locked(build_id)
        if row.state in {"active", "retired", "pruning", "pruned"}:
            raise BuildLifecycleError(f"Cannot fail build in state {row.state}")
        row.state = "failed"
        self.session.add(
            BuildDiagnosticRow(
                build_id=build_id,
                category="load-failure",
                severity="error",
                blocks_activation=True,
                details=details,
            )
        )
        self.session.flush()

    def add_validation(
        self,
        build_id: int,
        *,
        validator: str,
        passed: bool,
        report: dict[str, Any],
        advisory: bool = False,
    ) -> None:
        self._require_mutable(build_id)
        if validator.lower() in {"mobilitydata", "mobilitydata-gtfs-validator"}:
            advisory = True
        self.session.add(
            BuildValidationRow(
                build_id=build_id,
                validator=validator,
                advisory=advisory,
                passed=passed,
                report=report,
            )
        )
        self.session.flush()

    def add_diagnostic(
        self,
        build_id: int,
        *,
        category: str,
        severity: str,
        details: dict[str, Any],
        blocks_activation: bool,
    ) -> None:
        self._require_mutable(build_id)
        self.session.add(
            BuildDiagnosticRow(
                build_id=build_id,
                category=category,
                severity=severity,
                blocks_activation=blocks_activation,
                details=details,
            )
        )
        self.session.flush()

    def activate(self, build_id: int, *, publication: str = "public") -> None:
        row = self._locked(build_id)
        if row.state not in {"ready", "retired", "active"} or not row.partitions_attached:
            raise BuildLifecycleError("Only a retained, attached build can activate")
        if self._has_blocker(build_id):
            raise BuildLifecycleError("Build has blocking validation or diagnostics")
        pointer = self.session.execute(
            select(PublicationRow).where(PublicationRow.name == publication).with_for_update()
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if pointer is not None and pointer.active_build_id != build_id:
            previous = self._locked(pointer.active_build_id)
            other_publication = self.session.scalar(
                select(PublicationRow.name)
                .where(
                    PublicationRow.active_build_id == previous.id,
                    PublicationRow.name != publication,
                )
                .limit(1)
            )
            if other_publication is None:
                previous.state = "retired"
        row.state = "active"
        row.activated_at = now
        if pointer is None:
            self.session.add(
                PublicationRow(name=publication, active_build_id=build_id, updated_at=now)
            )
        else:
            pointer.active_build_id = build_id
            pointer.updated_at = now
        self.session.flush()

    def active_build_id(self, publication: str = "public") -> int:
        result = self.session.scalar(
            select(PublicationRow.active_build_id).where(PublicationRow.name == publication)
        )
        if result is None:
            raise BuildLifecycleError(f"Publication {publication!r} has no active build")
        return result

    def prune_after_activation(
        self, *, publication: str = "public", retain: int = 3
    ) -> tuple[int, ...]:
        if retain < 1:
            raise ValueError("At least the active build must be retained")
        active_id = self.active_build_id(publication)
        retained = self.session.scalars(
            select(StaticBuildRow)
            .where(
                StaticBuildRow.state.in_(("active", "retired")),
                StaticBuildRow.payload_pruned_at.is_(None),
            )
            .order_by(StaticBuildRow.activated_at.desc(), StaticBuildRow.id.desc())
            .with_for_update()
        ).all()
        published_ids = set(self.session.scalars(select(PublicationRow.active_build_id)).all())
        keep = {row.id for row in retained[:retain]} | published_ids | {active_id}
        pruned: list[int] = []
        for row in retained:
            if row.id in keep or row.state != "retired":
                continue
            row.state = "pruning"
            self.session.flush()
            for relation in reversed(STATIC_RELATIONS):
                partition = f"{relation}_b{row.id}"
                self.session.execute(text(f'DROP TABLE IF EXISTS static."{partition}"'))
            row.partitions_attached = False
            row.state = "pruned"
            row.payload_pruned_at = datetime.now(UTC)
            pruned.append(row.id)
        self.session.flush()
        return tuple(pruned)

    def _has_blocker(self, build_id: int) -> bool:
        failed_validation = self.session.scalar(
            select(BuildValidationRow.id).where(
                BuildValidationRow.build_id == build_id,
                BuildValidationRow.advisory.is_(False),
                BuildValidationRow.passed.is_(False),
            )
        )
        blocking_diagnostic = self.session.scalar(
            select(BuildDiagnosticRow.id).where(
                BuildDiagnosticRow.build_id == build_id,
                BuildDiagnosticRow.blocks_activation.is_(True),
            )
        )
        return failed_validation is not None or blocking_diagnostic is not None

    def _locked(self, build_id: int) -> StaticBuildRow:
        row = self.session.execute(
            select(StaticBuildRow).where(StaticBuildRow.id == build_id).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise BuildLifecycleError(f"Unknown static build {build_id}")
        return row

    def _require_mutable(self, build_id: int) -> StaticBuildRow:
        row = self._locked(build_id)
        if row.state not in {"building", "loading"}:
            raise BuildLifecycleError(f"Build {build_id} is no longer mutable")
        return row
