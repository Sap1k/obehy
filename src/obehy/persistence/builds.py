from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from obehy.persistence.models import (
    BoardingPointRevisionRow,
    BuildDiagnosticRow,
    BuildValidationRow,
    FareZoneRevisionRow,
    OperationalPointRevisionRow,
    OperatorRevisionRow,
    PublicationRow,
    RouteRevisionRow,
    SelectedFieldProvenanceRow,
    ServiceCalendarRow,
    ShapePointRow,
    ShapeRow,
    StaticBuildInputRow,
    StaticBuildRow,
    StopPlaceRevisionRow,
    StopZoneAssignmentRow,
    TransferRevisionRow,
    TripCallRevisionRow,
    TripCallZoneAssignmentRow,
    TripRevisionRow,
)


class BuildLifecycleError(RuntimeError):
    pass


class BuildService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        version: str,
        config_sha256: str,
        compiler_version: str,
        manifest: dict[str, Any] | None = None,
    ) -> int:
        row = StaticBuildRow(
            version=version,
            state="building",
            config_sha256=config_sha256,
            compiler_version=compiler_version,
            manifest=manifest or {},
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def add_input(self, build_id: int, snapshot_id: int, role: str) -> None:
        self._require_state(build_id, "building")
        self.session.add(StaticBuildInputRow(build_id=build_id, snapshot_id=snapshot_id, role=role))
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
        self._require_state(build_id, "building")
        if validator.lower() in {"mobilitydata", "mobilitydata-gtfs-validator"}:
            advisory = True
        self.session.add(
            BuildValidationRow(
                build_id=build_id,
                validator=validator,
                is_advisory=advisory,
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
        self._require_state(build_id, "building")
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

    def mark_ready(self, build_id: int, *, output_artifact_key: str) -> None:
        row = self._locked(build_id)
        if row.state != "building":
            raise BuildLifecycleError("Only a building build can become ready")
        blocking_validation = self.session.scalar(
            select(BuildValidationRow.id).where(
                BuildValidationRow.build_id == build_id,
                BuildValidationRow.is_advisory.is_(False),
                BuildValidationRow.passed.is_(False),
            )
        )
        blocking_diagnostic = self.session.scalar(
            select(BuildDiagnosticRow.id).where(
                BuildDiagnosticRow.build_id == build_id,
                BuildDiagnosticRow.blocks_activation.is_(True),
            )
        )
        if blocking_validation is not None or blocking_diagnostic is not None:
            raise BuildLifecycleError("Build has blocking validation or diagnostics")
        row.output_artifact_key = output_artifact_key
        row.ready_at = datetime.now(UTC)
        row.state = "ready"
        self.session.flush()

    def activate(self, build_id: int, *, publication: str = "public") -> None:
        row = self._locked(build_id)
        if row.state not in {"ready", "retired"} or row.payload_pruned_at is not None:
            raise BuildLifecycleError("Only a retained ready or retired build can be activated")
        pointer = self.session.execute(
            select(PublicationRow).where(PublicationRow.name == publication).with_for_update()
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if pointer is not None and pointer.active_build_id != build_id:
            previous = self._locked(pointer.active_build_id)
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
        build_id = self.session.scalar(
            select(PublicationRow.active_build_id).where(PublicationRow.name == publication)
        )
        if build_id is None:
            raise BuildLifecycleError(f"Publication {publication!r} has no active build")
        return build_id

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
        keep = {row.id for row in retained[:retain]} | {active_id}
        candidates = [row for row in retained if row.id not in keep and row.state == "retired"]
        pruned: list[int] = []
        for row in candidates:
            row.state = "pruning"
            self.session.flush()
            self._delete_payload(row.id)
            row.state = "pruned"
            row.payload_pruned_at = datetime.now(UTC)
            pruned.append(row.id)
        self.session.flush()
        return tuple(pruned)

    def _delete_payload(self, build_id: int) -> None:
        child_first = (
            TripCallZoneAssignmentRow,
            StopZoneAssignmentRow,
            FareZoneRevisionRow,
            TransferRevisionRow,
            TripCallRevisionRow,
            TripRevisionRow,
            ShapePointRow,
            ShapeRow,
            RouteRevisionRow,
            BoardingPointRevisionRow,
            StopPlaceRevisionRow,
            OperationalPointRevisionRow,
            OperatorRevisionRow,
            SelectedFieldProvenanceRow,
            ServiceCalendarRow,
        )
        for model in child_first:
            self.session.execute(delete(model).where(model.build_id == build_id))

    def _locked(self, build_id: int) -> StaticBuildRow:
        row = self.session.execute(
            select(StaticBuildRow).where(StaticBuildRow.id == build_id).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise BuildLifecycleError(f"Unknown static build {build_id}")
        return row

    def _require_state(self, build_id: int, wanted: str) -> StaticBuildRow:
        row = self._locked(build_id)
        if row.state != wanted:
            raise BuildLifecycleError(f"Build {build_id} must be {wanted}, got {row.state}")
        return row
