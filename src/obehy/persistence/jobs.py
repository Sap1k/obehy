from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from obehy.persistence.models import BuildJobAttemptRow, BuildJobEventRow, BuildJobRow


class JobStateError(RuntimeError):
    pass


class BuildJobService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(self, build_spec_sha256: str, *, priority: int = 0) -> int:
        row = BuildJobRow(build_spec_sha256=build_spec_sha256, priority=priority, state="queued")
        self.session.add(row)
        self.session.flush()
        self._event(row.id, "queued", {})
        return row.id

    def claim(self, worker_id: str) -> tuple[int, int] | None:
        job = self.session.execute(
            select(BuildJobRow)
            .where(BuildJobRow.state == "queued", BuildJobRow.cancel_requested.is_(False))
            .order_by(BuildJobRow.priority.desc(), BuildJobRow.created_at, BuildJobRow.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if job is None:
            return None
        previous = self.session.scalar(
            select(func.max(BuildJobAttemptRow.attempt)).where(BuildJobAttemptRow.job_id == job.id)
        )
        attempt = (previous or 0) + 1
        job.state = "running"
        self.session.add(BuildJobAttemptRow(job_id=job.id, attempt=attempt, worker_id=worker_id))
        self._event(job.id, "claimed", {"attempt": attempt, "worker_id": worker_id})
        self.session.flush()
        return job.id, attempt

    def heartbeat(self, job_id: int, attempt: int, progress: dict[str, Any]) -> bool:
        job = self._locked(job_id)
        row = self.session.get(BuildJobAttemptRow, (job_id, attempt))
        if job.state != "running" or row is None or row.finished_at is not None:
            raise JobStateError("Only the active attempt can heartbeat")
        row.heartbeat_at = datetime.now(UTC)
        self._event(job_id, "progress", progress)
        self.session.flush()
        return job.cancel_requested

    def request_cancel(self, job_id: int) -> None:
        job = self._locked(job_id)
        if job.state not in {"queued", "running"}:
            raise JobStateError("Only queued or running jobs can be cancelled")
        job.cancel_requested = True
        if job.state == "queued":
            job.state = "cancelled"
            job.finished_at = datetime.now(UTC)
        self._event(job_id, "cancel-requested", {})
        self.session.flush()

    def finish(
        self,
        job_id: int,
        attempt: int,
        *,
        succeeded: bool,
        exit_code: int,
        error: dict[str, Any] | None = None,
        log_artifact_sha256: str | None = None,
    ) -> None:
        job = self._locked(job_id)
        row = self.session.get(BuildJobAttemptRow, (job_id, attempt))
        if job.state != "running" or row is None or row.finished_at is not None:
            raise JobStateError("Only the active attempt can finish")
        now = datetime.now(UTC)
        row.finished_at = now
        row.heartbeat_at = now
        row.exit_code = exit_code
        row.error = error
        row.log_artifact_sha256 = log_artifact_sha256
        job.state = (
            "cancelled" if job.cancel_requested else ("succeeded" if succeeded else "failed")
        )
        job.finished_at = now
        self._event(job_id, job.state, {"attempt": attempt, "exit_code": exit_code})
        self.session.flush()

    def retry(self, job_id: int) -> None:
        job = self._locked(job_id)
        if job.state != "failed":
            raise JobStateError("Only failed jobs can be retried")
        job.state = "queued"
        job.finished_at = None
        job.cancel_requested = False
        self._event(job_id, "retried", {})
        self.session.flush()

    def _event(self, job_id: int, event_type: str, details: dict[str, Any]) -> None:
        self.session.add(BuildJobEventRow(job_id=job_id, event_type=event_type, details=details))

    def _locked(self, job_id: int) -> BuildJobRow:
        row = self.session.execute(
            select(BuildJobRow).where(BuildJobRow.id == job_id).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise JobStateError(f"Unknown build job {job_id}")
        return row
