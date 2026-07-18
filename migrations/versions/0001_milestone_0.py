"""Milestone 0 canonical domain contracts.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DDL = (
    "CREATE EXTENSION IF NOT EXISTS postgis",
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
    "CREATE SEQUENCE canonical_stop_place_seq MINVALUE 1 MAXVALUE 999999999 NO CYCLE",
    "CREATE SEQUENCE canonical_boarding_point_seq MINVALUE 1 MAXVALUE 999999999 NO CYCLE",
    "CREATE SEQUENCE canonical_operational_point_seq MINVALUE 1 MAXVALUE 999999999 NO CYCLE",
    "CREATE SEQUENCE canonical_route_seq MINVALUE 1 MAXVALUE 999999999 NO CYCLE",
    "CREATE SEQUENCE canonical_scheduled_trip_seq MINVALUE 1 MAXVALUE 999999999 NO CYCLE",
    """
    CREATE TABLE canonical_entity (
        id varchar(10) PRIMARY KEY,
        kind varchar(32) NOT NULL,
        status varchar(16) NOT NULL DEFAULT 'active',
        redirect_to_id varchar(10) REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_canonical_entity_kind CHECK (
            kind IN ('stop_place','boarding_point','operational_point','route','scheduled_trip')
        ),
        CONSTRAINT ck_canonical_entity_status CHECK (
            status IN ('active','tombstoned','redirected')
        ),
        CONSTRAINT ck_canonical_entity_redirect_state CHECK (
            (status = 'redirected' AND redirect_to_id IS NOT NULL) OR
            (status <> 'redirected' AND redirect_to_id IS NULL)
        ),
        CONSTRAINT ck_no_self_redirect CHECK (redirect_to_id IS NULL OR redirect_to_id <> id)
    )
    """,
    """
    CREATE TABLE source (
        id varchar(64) PRIMARY KEY,
        display_name varchar(160) NOT NULL,
        adapter_kind varchar(32) NOT NULL,
        timezone varchar(64) NOT NULL DEFAULT 'Europe/Prague',
        active boolean NOT NULL DEFAULT true
    )
    """,
    """
    CREATE TABLE source_binding (
        id bigserial PRIMARY KEY,
        source_id varchar(64) NOT NULL REFERENCES source(id),
        entity_kind varchar(32) NOT NULL,
        source_object_id text NOT NULL,
        canonical_entity_id varchar(10) NOT NULL REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        validity tstzrange NOT NULL,
        match_method varchar(32) NOT NULL,
        match_confidence double precision NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        reviewed_by varchar(160),
        CONSTRAINT ck_source_binding_nonempty CHECK (NOT isempty(validity)),
        CONSTRAINT ck_source_binding_confidence CHECK (
            match_confidence >= 0 AND match_confidence <= 1
        ),
        CONSTRAINT ex_source_binding_no_overlap EXCLUDE USING gist (
            source_id WITH =, entity_kind WITH =, source_object_id WITH =, validity WITH &&
        )
    )
    """,
    """
    CREATE TABLE identifier_alias (
        id bigserial PRIMARY KEY,
        source_id varchar(64) NOT NULL REFERENCES source(id),
        identifier_kind varchar(64) NOT NULL,
        observed_value text NOT NULL,
        normalized_value text NOT NULL,
        validity tstzrange NOT NULL,
        reason text NOT NULL,
        CONSTRAINT ck_identifier_alias_nonempty CHECK (NOT isempty(validity)),
        CONSTRAINT ex_identifier_alias_no_overlap EXCLUDE USING gist (
            source_id WITH =, identifier_kind WITH =, observed_value WITH =, validity WITH &&
        )
    )
    """,
    """
    CREATE TABLE identity_diagnostic (
        id bigserial PRIMARY KEY,
        source_id varchar(64) NOT NULL REFERENCES source(id),
        entity_kind varchar(32) NOT NULL,
        source_object_id text NOT NULL,
        effective_at timestamptz NOT NULL,
        error_category varchar(64) NOT NULL,
        candidate_ids jsonb NOT NULL,
        details jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE stop_place (
        id varchar(10) PRIMARY KEY REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        name text NOT NULL,
        centroid geometry(POINT, 4326)
    )
    """,
    """
    CREATE TABLE boarding_point (
        id varchar(10) PRIMARY KEY REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        stop_place_id varchar(10) NOT NULL REFERENCES stop_place(id) ON DELETE RESTRICT,
        name text,
        source_code text,
        is_unspecified boolean NOT NULL DEFAULT false,
        position geometry(POINT, 4326)
    )
    """,
    "CREATE UNIQUE INDEX uq_boarding_point_unspecified_per_place "
    "ON boarding_point(stop_place_id) WHERE is_unspecified",
    """
    CREATE TABLE operational_point (
        id varchar(10) PRIMARY KEY REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        name text NOT NULL,
        code text,
        position geometry(POINT, 4326)
    )
    """,
    """
    CREATE TABLE canonical_route (
        id varchar(10) PRIMARY KEY REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        mode varchar(24) NOT NULL,
        cis_line_id varchar(6),
        public_name text,
        CONSTRAINT ck_route_cis_line_id CHECK (
            cis_line_id IS NULL OR cis_line_id ~ '^[0-9]{6}$'
        ),
        CONSTRAINT uq_route_cis_line_id UNIQUE (cis_line_id)
    )
    """,
    """
    CREATE TABLE service_calendar (
        id bigserial PRIMARY KEY,
        valid_from date NOT NULL,
        valid_to date NOT NULL,
        weekday_mask integer NOT NULL,
        CONSTRAINT ck_calendar_validity CHECK (valid_to >= valid_from),
        CONSTRAINT ck_weekday_mask CHECK (weekday_mask >= 0 AND weekday_mask <= 127)
    )
    """,
    """
    CREATE TABLE service_exception (
        calendar_id bigint NOT NULL REFERENCES service_calendar(id) ON DELETE CASCADE,
        service_date date NOT NULL,
        added boolean NOT NULL,
        PRIMARY KEY (calendar_id, service_date)
    )
    """,
    """
    CREATE TABLE scheduled_trip (
        id varchar(10) PRIMARY KEY REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        route_id varchar(10) NOT NULL REFERENCES canonical_route(id),
        mode varchar(24) NOT NULL,
        direction integer NOT NULL,
        calendar_id bigint NOT NULL REFERENCES service_calendar(id),
        timetable_variant text NOT NULL,
        cis_line_id varchar(6),
        cis_trip_id bigint,
        train_number integer,
        CONSTRAINT ck_scheduled_trip_direction CHECK (direction IN (0, 1)),
        CONSTRAINT ck_scheduled_trip_identity CHECK (
            (cis_line_id IS NOT NULL AND cis_trip_id IS NOT NULL AND train_number IS NULL) OR
            (cis_line_id IS NULL AND cis_trip_id IS NULL AND train_number IS NOT NULL)
        ),
        CONSTRAINT ck_trip_cis_line_id CHECK (
            cis_line_id IS NULL OR cis_line_id ~ '^[0-9]{6}$'
        ),
        CONSTRAINT ck_trip_cis_trip_id CHECK (cis_trip_id IS NULL OR cis_trip_id >= 0),
        CONSTRAINT ck_trip_train_number CHECK (train_number IS NULL OR train_number > 0)
    )
    """,
    """
    CREATE TABLE trip_call (
        trip_id varchar(10) NOT NULL REFERENCES scheduled_trip(id) ON DELETE CASCADE,
        sequence integer NOT NULL,
        location_id varchar(10) NOT NULL REFERENCES canonical_entity(id) ON DELETE RESTRICT,
        passenger_service boolean NOT NULL,
        scheduled_boarding_point_id varchar(10) REFERENCES boarding_point(id) ON DELETE RESTRICT,
        scheduled_arrival integer,
        scheduled_departure integer,
        scheduled_passage integer,
        pickup_allowed boolean NOT NULL DEFAULT false,
        dropoff_allowed boolean NOT NULL DEFAULT false,
        PRIMARY KEY (trip_id, sequence),
        CONSTRAINT ck_trip_call_sequence CHECK (sequence > 0),
        CONSTRAINT ck_call_arrival CHECK (scheduled_arrival IS NULL OR scheduled_arrival >= 0),
        CONSTRAINT ck_call_departure CHECK (
            scheduled_departure IS NULL OR scheduled_departure >= 0
        ),
        CONSTRAINT ck_call_passage CHECK (scheduled_passage IS NULL OR scheduled_passage >= 0),
        CONSTRAINT ck_trip_call_shape CHECK (
            (passenger_service AND scheduled_boarding_point_id IS NOT NULL
             AND scheduled_passage IS NULL
             AND (scheduled_arrival IS NOT NULL OR scheduled_departure IS NOT NULL)) OR
            (NOT passenger_service AND scheduled_boarding_point_id IS NULL
             AND scheduled_arrival IS NULL AND scheduled_departure IS NULL
             AND scheduled_passage IS NOT NULL AND NOT pickup_allowed AND NOT dropoff_allowed)
        )
    )
    """,
    """
    CREATE FUNCTION validate_canonical_child_kind() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE actual_kind text;
    BEGIN
        SELECT kind INTO actual_kind FROM canonical_entity WHERE id = NEW.id;
        IF actual_kind <> TG_ARGV[0] THEN
            RAISE EXCEPTION 'canonical child % must have kind %, got %',
                NEW.id, TG_ARGV[0], actual_kind;
        END IF;
        RETURN NEW;
    END $$
    """,
    "CREATE TRIGGER stop_place_kind BEFORE INSERT OR UPDATE ON stop_place FOR EACH ROW "
    "EXECUTE FUNCTION validate_canonical_child_kind('stop_place')",
    "CREATE TRIGGER boarding_point_kind BEFORE INSERT OR UPDATE ON boarding_point FOR EACH ROW "
    "EXECUTE FUNCTION validate_canonical_child_kind('boarding_point')",
    "CREATE TRIGGER operational_point_kind BEFORE INSERT OR UPDATE "
    "ON operational_point FOR EACH ROW "
    "EXECUTE FUNCTION validate_canonical_child_kind('operational_point')",
    "CREATE TRIGGER route_kind BEFORE INSERT OR UPDATE ON canonical_route FOR EACH ROW "
    "EXECUTE FUNCTION validate_canonical_child_kind('route')",
    "CREATE TRIGGER scheduled_trip_kind BEFORE INSERT OR UPDATE ON scheduled_trip FOR EACH ROW "
    "EXECUTE FUNCTION validate_canonical_child_kind('scheduled_trip')",
    """
    CREATE FUNCTION validate_trip_call_location() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE location_kind text;
    DECLARE boarding_parent text;
    BEGIN
        SELECT kind INTO location_kind FROM canonical_entity WHERE id = NEW.location_id;
        IF NEW.passenger_service THEN
            IF location_kind <> 'stop_place' THEN
                RAISE EXCEPTION 'passenger call location must be a stop place';
            END IF;
            SELECT stop_place_id INTO boarding_parent
              FROM boarding_point WHERE id = NEW.scheduled_boarding_point_id;
            IF boarding_parent IS DISTINCT FROM NEW.location_id THEN
                RAISE EXCEPTION 'boarding point must be a child of the call stop place';
            END IF;
        ELSIF location_kind <> 'operational_point' THEN
            RAISE EXCEPTION 'operational call location must be an operational point';
        END IF;
        RETURN NEW;
    END $$
    """,
    "CREATE TRIGGER trip_call_location BEFORE INSERT OR UPDATE ON trip_call FOR EACH ROW "
    "EXECUTE FUNCTION validate_trip_call_location()",
)


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)
    op.bulk_insert(
        sa.table(
            "source",
            sa.column("id", sa.String),
            sa.column("display_name", sa.String),
            sa.column("adapter_kind", sa.String),
        ),
        [
            {"id": "national-jdf", "display_name": "National JDF", "adapter_kind": "jdf"},
            {"id": "national-czptt", "display_name": "National CZPTT", "adapter_kind": "czptt"},
            {"id": "pid", "display_name": "PID", "adapter_kind": "gtfs"},
            {"id": "duk", "display_name": "DÚK", "adapter_kind": "custom_realtime"},
            {
                "id": "cis-authority-mock",
                "display_name": "Synthetic CIS authority",
                "adapter_kind": "fixture_authority",
            },
        ],
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trip_call")
    op.execute("DROP FUNCTION IF EXISTS validate_trip_call_location()")
    for table in (
        "scheduled_trip",
        "service_exception",
        "service_calendar",
        "canonical_route",
        "operational_point",
        "boarding_point",
        "stop_place",
        "identity_diagnostic",
        "identifier_alias",
        "source_binding",
        "source",
        "canonical_entity",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS validate_canonical_child_kind()")
    for sequence in (
        "canonical_scheduled_trip_seq",
        "canonical_route_seq",
        "canonical_operational_point_seq",
        "canonical_boarding_point_seq",
        "canonical_stop_place_seq",
    ):
        op.execute(f"DROP SEQUENCE IF EXISTS {sequence}")
