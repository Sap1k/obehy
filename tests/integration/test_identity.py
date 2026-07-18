from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from obehy.domain.identifiers import EntityKind
from obehy.identity.services import (
    AliasService,
    AmbiguousIdentityError,
    BindingRequest,
    CanonicalRegistry,
    InvalidLifecycleChangeError,
    SourceIdentityService,
    bind_many_with_diagnostic,
)
from obehy.persistence.database import create_session_factory
from obehy.persistence.models import CanonicalEntityRow, IdentityDiagnosticRow

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 18, tzinfo=UTC)


def test_allocator_lifecycle_and_non_recycling(db_session: Session) -> None:
    registry = CanonicalRegistry(db_session)
    source = registry.allocate(EntityKind.STOP_PLACE)
    target = registry.allocate(EntityKind.STOP_PLACE)
    assert str(source).startswith("S") and source != target
    assert registry.redirect(source, target) == target
    assert registry.resolve_terminal(source) == target
    with pytest.raises(InvalidLifecycleChangeError):
        registry.redirect(target, source)
    registry.tombstone(target)
    assert registry.resolve_terminal(source) == target


def test_rolled_back_allocation_leaves_a_sequence_gap(engine: Engine) -> None:
    factory = create_session_factory(engine)
    consumed = None
    with pytest.raises(RuntimeError), factory.begin() as session:
        consumed = CanonicalRegistry(session).allocate(EntityKind.OPERATIONAL_POINT)
        raise RuntimeError("force rollback")
    assert consumed is not None
    with factory.begin() as session:
        following = CanonicalRegistry(session).allocate(EntityKind.OPERATIONAL_POINT)
        assert int(following.value[1:]) > int(consumed.value[1:])


def test_concurrent_allocations_are_unique(engine: Engine) -> None:
    factory = create_session_factory(engine)

    def allocate_one() -> str:
        with factory.begin() as session:
            return str(CanonicalRegistry(session).allocate(EntityKind.ROUTE))

    def allocate_ignored(_: int) -> str:
        return allocate_one()

    with ThreadPoolExecutor(max_workers=4) as executor:
        identifiers = list(executor.map(allocate_ignored, range(8)))
    assert len(set(identifiers)) == 8


def test_binding_boundaries_idempotency_and_redirect_resolution(db_session: Session) -> None:
    registry = CanonicalRegistry(db_session)
    old = registry.allocate(EntityKind.STOP_PLACE)
    current = registry.allocate(EntityKind.STOP_PLACE)
    service = SourceIdentityService(db_session)
    request = BindingRequest(
        "national-jdf",
        EntityKind.STOP_PLACE,
        "export-stop-1",
        old,
        NOW,
        NOW + timedelta(days=1),
        "authoritative_identifier",
        0.99,
    )
    assert service.bind(request) == old
    assert service.bind(request) == old
    assert service.resolve("national-jdf", EntityKind.STOP_PLACE, "export-stop-1", NOW) == old
    assert (
        service.resolve(
            "national-jdf", EntityKind.STOP_PLACE, "export-stop-1", NOW + timedelta(days=1)
        )
        is None
    )
    registry.redirect(old, current)
    assert service.resolve("national-jdf", EntityKind.STOP_PLACE, "export-stop-1", NOW) == current


def test_duk_alias_is_typed_time_bounded_and_non_chained(db_session: Session) -> None:
    aliases = AliasService(db_session)
    aliases.add(
        source_id="duk",
        identifier_kind="cis_line_id",
        observed_value="582588",
        normalized_value="001588",
        valid_from=NOW,
        valid_to=None,
        reason="DÚK API-specific encoding",
    )
    assert aliases.normalize_cis_line("duk", "582588", NOW).value == "001588"
    with pytest.raises(AmbiguousIdentityError):
        aliases.add(
            source_id="duk",
            identifier_kind="cis_line_id",
            observed_value="582588",
            normalized_value="999999",
            valid_from=NOW + timedelta(hours=1),
            valid_to=None,
            reason="conflict",
        )


def test_conflicting_binding_rolls_back_and_records_diagnostic(engine: Engine) -> None:
    factory = create_session_factory(engine)
    source_object_id = f"ambiguous-{uuid4()}"
    with factory.begin() as session:
        first = CanonicalRegistry(session).allocate(EntityKind.STOP_PLACE)
        second = CanonicalRegistry(session).allocate(EntityKind.STOP_PLACE)
        valid_target = CanonicalRegistry(session).allocate(EntityKind.STOP_PLACE)
        SourceIdentityService(session).bind(
            BindingRequest(
                "national-jdf",
                EntityKind.STOP_PLACE,
                source_object_id,
                first,
                NOW,
                None,
                "manual",
                1.0,
            )
        )
    valid_source_object_id = f"valid-but-rolled-back-{uuid4()}"
    with pytest.raises(AmbiguousIdentityError):
        bind_many_with_diagnostic(
            factory,
            (
                BindingRequest(
                    "national-jdf",
                    EntityKind.STOP_PLACE,
                    valid_source_object_id,
                    valid_target,
                    NOW,
                    None,
                    "manual",
                    1.0,
                ),
                BindingRequest(
                    "national-jdf",
                    EntityKind.STOP_PLACE,
                    source_object_id,
                    second,
                    NOW + timedelta(minutes=1),
                    None,
                    "manual",
                    1.0,
                ),
            ),
        )
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdentityDiagnosticRow)
                .where(IdentityDiagnosticRow.source_object_id == source_object_id)
            )
            == 1
        )
        assert session.get(CanonicalEntityRow, str(second)) is not None
        assert (
            SourceIdentityService(session).resolve(
                "national-jdf",
                EntityKind.STOP_PLACE,
                valid_source_object_id,
                NOW,
            )
            is None
        )
