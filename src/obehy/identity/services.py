from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session, sessionmaker

from obehy.domain.identifiers import CanonicalId, CisLineId, EntityKind
from obehy.persistence.models import (
    ID_SEQUENCES,
    CanonicalEntityRow,
    IdentifierAliasRow,
    IdentityDiagnosticRow,
    SourceBindingRow,
)


class IdentityError(RuntimeError):
    pass


class UnknownCanonicalEntityError(IdentityError):
    pass


class InvalidLifecycleChangeError(IdentityError):
    pass


class AmbiguousIdentityError(IdentityError):
    def __init__(self, message: str, candidate_ids: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.candidate_ids = candidate_ids


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Binding and alias instants must be timezone-aware")


def validity_range(valid_from: datetime, valid_to: datetime | None) -> Range[datetime]:
    _aware(valid_from)
    if valid_to is not None:
        _aware(valid_to)
        if valid_to <= valid_from:
            raise ValueError("Validity end must be later than validity start")
    return Range(valid_from, valid_to, bounds="[)")


class CanonicalRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session

    def allocate(self, kind: EntityKind) -> CanonicalId:
        number = self.session.execute(select(ID_SEQUENCES[kind].next_value())).scalar_one()
        canonical_id = CanonicalId.from_number(kind, number)
        self.session.add(CanonicalEntityRow(id=str(canonical_id), kind=kind.value, status="active"))
        self.session.flush()
        return canonical_id

    def _locked(self, canonical_id: CanonicalId) -> CanonicalEntityRow:
        row = self.session.execute(
            select(CanonicalEntityRow)
            .where(CanonicalEntityRow.id == str(canonical_id))
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise UnknownCanonicalEntityError(str(canonical_id))
        return row

    def resolve_terminal(self, canonical_id: CanonicalId) -> CanonicalId:
        seen: set[str] = set()
        current = str(canonical_id)
        while True:
            if current in seen:
                raise InvalidLifecycleChangeError("Canonical redirect cycle detected")
            seen.add(current)
            row = self.session.get(CanonicalEntityRow, current)
            if row is None:
                raise UnknownCanonicalEntityError(current)
            if row.status != "redirected":
                return CanonicalId(row.id)
            if row.redirect_to_id is None:
                raise InvalidLifecycleChangeError("Redirected entity has no target")
            current = row.redirect_to_id

    def tombstone(self, canonical_id: CanonicalId) -> None:
        row = self._locked(canonical_id)
        if row.status != "active":
            raise InvalidLifecycleChangeError("Only an active canonical entity can be tombstoned")
        row.status = "tombstoned"
        self.session.flush()

    def redirect(self, source_id: CanonicalId, target_id: CanonicalId) -> CanonicalId:
        if source_id == target_id:
            raise InvalidLifecycleChangeError("A canonical entity cannot redirect to itself")
        source = self._locked(source_id)
        if source.status != "active":
            raise InvalidLifecycleChangeError("Only an active canonical entity can be redirected")
        terminal_id = self.resolve_terminal(target_id)
        if terminal_id == source_id:
            raise InvalidLifecycleChangeError("Canonical redirect would create a cycle")
        target = self._locked(terminal_id)
        if target.status != "active":
            raise InvalidLifecycleChangeError("Redirect target must be active")
        if source.kind != target.kind:
            raise InvalidLifecycleChangeError("Redirect source and target must have the same kind")
        source.status = "redirected"
        source.redirect_to_id = target.id
        self.session.flush()
        return terminal_id


@dataclass(frozen=True, slots=True)
class BindingRequest:
    source_id: str
    entity_kind: EntityKind
    source_object_id: str
    canonical_entity_id: CanonicalId
    valid_from: datetime
    valid_to: datetime | None
    match_method: str
    match_confidence: float
    reviewed_by: str | None = None


class SourceIdentityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def bind(self, request: BindingRequest) -> CanonicalId:
        if request.canonical_entity_id.kind is not request.entity_kind:
            raise ValueError("Binding entity kind does not match canonical ID prefix")
        if not 0 <= request.match_confidence <= 1:
            raise ValueError("Match confidence must be between zero and one")
        validity = validity_range(request.valid_from, request.valid_to)
        overlaps = self.session.scalars(
            select(SourceBindingRow).where(
                SourceBindingRow.source_id == request.source_id,
                SourceBindingRow.entity_kind == request.entity_kind.value,
                SourceBindingRow.source_object_id == request.source_object_id,
                SourceBindingRow.validity.overlaps(validity),
            )
        ).all()
        exact = [
            row
            for row in overlaps
            if row.canonical_entity_id == str(request.canonical_entity_id)
            and row.validity == validity
        ]
        if len(overlaps) == 1 and exact:
            return request.canonical_entity_id
        if overlaps:
            raise AmbiguousIdentityError(
                "Source identifier already has an overlapping canonical mapping",
                tuple(
                    sorted(
                        {row.canonical_entity_id for row in overlaps}
                        | {str(request.canonical_entity_id)}
                    )
                ),
            )
        entity = self.session.get(CanonicalEntityRow, str(request.canonical_entity_id))
        if entity is None or entity.kind != request.entity_kind.value:
            raise UnknownCanonicalEntityError(str(request.canonical_entity_id))
        self.session.add(
            SourceBindingRow(
                source_id=request.source_id,
                entity_kind=request.entity_kind.value,
                source_object_id=request.source_object_id,
                canonical_entity_id=str(request.canonical_entity_id),
                validity=validity,
                match_method=request.match_method,
                match_confidence=request.match_confidence,
                reviewed_by=request.reviewed_by,
            )
        )
        self.session.flush()
        return request.canonical_entity_id

    def resolve(
        self, source_id: str, entity_kind: EntityKind, source_object_id: str, effective_at: datetime
    ) -> CanonicalId | None:
        _aware(effective_at)
        rows = self.session.scalars(
            select(SourceBindingRow).where(
                SourceBindingRow.source_id == source_id,
                SourceBindingRow.entity_kind == entity_kind.value,
                SourceBindingRow.source_object_id == source_object_id,
                SourceBindingRow.validity.contains(effective_at),
            )
        ).all()
        if len(rows) > 1:
            raise AmbiguousIdentityError(
                "Source identifier resolved to multiple canonical entities",
                tuple(sorted(row.canonical_entity_id for row in rows)),
            )
        if not rows:
            return None
        return CanonicalRegistry(self.session).resolve_terminal(
            CanonicalId(rows[0].canonical_entity_id)
        )


def bind_many_with_diagnostic(
    factory: sessionmaker[Session], requests: tuple[BindingRequest, ...]
) -> tuple[CanonicalId, ...]:
    active_request: BindingRequest | None = None
    try:
        with factory.begin() as session:
            service = SourceIdentityService(session)
            results: list[CanonicalId] = []
            for active_request in requests:
                results.append(service.bind(active_request))
            return tuple(results)
    except AmbiguousIdentityError as error:
        if active_request is None:
            raise
        with factory.begin() as diagnostic_session:
            diagnostic_session.add(
                IdentityDiagnosticRow(
                    source_id=active_request.source_id,
                    entity_kind=active_request.entity_kind.value,
                    source_object_id=active_request.source_object_id,
                    effective_at=active_request.valid_from,
                    error_category="ambiguous_source_binding",
                    candidate_ids=list(error.candidate_ids),
                    details={"message": str(error)},
                )
            )
        raise


def bind_with_diagnostic(factory: sessionmaker[Session], request: BindingRequest) -> CanonicalId:
    return bind_many_with_diagnostic(factory, (request,))[0]


class AliasService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        source_id: str,
        identifier_kind: str,
        observed_value: str,
        normalized_value: str,
        valid_from: datetime,
        valid_to: datetime | None,
        reason: str,
    ) -> None:
        validity = validity_range(valid_from, valid_to)
        overlaps = self.session.scalars(
            select(IdentifierAliasRow).where(
                IdentifierAliasRow.source_id == source_id,
                IdentifierAliasRow.identifier_kind == identifier_kind,
                IdentifierAliasRow.observed_value == observed_value,
                IdentifierAliasRow.validity.overlaps(validity),
            )
        ).all()
        if (
            len(overlaps) == 1
            and overlaps[0].validity == validity
            and overlaps[0].normalized_value == normalized_value
        ):
            return
        if overlaps:
            raise AmbiguousIdentityError("Identifier alias has an overlapping definition")
        self.session.add(
            IdentifierAliasRow(
                source_id=source_id,
                identifier_kind=identifier_kind,
                observed_value=observed_value,
                normalized_value=normalized_value,
                validity=validity,
                reason=reason,
            )
        )
        self.session.flush()

    def normalize(
        self, source_id: str, identifier_kind: str, observed_value: str, effective_at: datetime
    ) -> str:
        _aware(effective_at)
        rows = self.session.scalars(
            select(IdentifierAliasRow).where(
                IdentifierAliasRow.source_id == source_id,
                IdentifierAliasRow.identifier_kind == identifier_kind,
                IdentifierAliasRow.observed_value == observed_value,
                IdentifierAliasRow.validity.contains(effective_at),
            )
        ).all()
        if len(rows) > 1:
            raise AmbiguousIdentityError("Identifier alias resolved ambiguously")
        return observed_value if not rows else rows[0].normalized_value

    def normalize_cis_line(
        self, source_id: str, observed_value: str, effective_at: datetime
    ) -> CisLineId:
        return CisLineId(self.normalize(source_id, "cis_line_id", observed_value, effective_at))
