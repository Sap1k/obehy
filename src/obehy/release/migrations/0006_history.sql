-- History (BASE_PLAN.md section 29, docs/R1_SLICE.md section 3). Self-describing: nothing here
-- references static.*. Keyed by journey (feed, key_namespace, key, service_date), partitioned
-- monthly by service_date; partitions are created on demand by the writer.
CREATE SCHEMA history;

CREATE TABLE history.derivation (
    derivation_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    core_version text NOT NULL,
    policy_version text NOT NULL,
    release_id text NOT NULL,
    UNIQUE (core_version, policy_version, release_id)
);

CREATE TABLE history.journey (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    first_bound_at timestamptz NOT NULL,
    latest_revision integer NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date)
) PARTITION BY RANGE (service_date);

CREATE TABLE history.journey_schedule (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    revision integer NOT NULL,
    release_id text NOT NULL,
    trip_id text NOT NULL,
    route_name text NOT NULL,
    headsign text,
    derivation_id integer NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date, revision)
) PARTITION BY RANGE (service_date);

CREATE TABLE history.journey_call (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    revision integer NOT NULL,
    ordinal integer NOT NULL,
    location_id text NOT NULL,
    visit_n integer NOT NULL,
    passenger_service boolean NOT NULL,
    scheduled_arrival integer,
    scheduled_departure integer,
    name text NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date, revision, ordinal)
) PARTITION BY RANGE (service_date);

-- Events attach to (location_id, visit_n), never to an ordinal. An event whose call vanished in
-- a later revision keeps its revision and is flagged orphaned; it is never moved or deleted.
CREATE TABLE history.actual_stop_event (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    location_id text NOT NULL,
    visit_n integer NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('arrival', 'departure', 'passage')),
    revision integer NOT NULL,
    event_time timestamptz NOT NULL,
    interval_lo timestamptz NOT NULL,
    interval_hi timestamptz NOT NULL,
    method text NOT NULL,
    source text NOT NULL,
    orphaned boolean NOT NULL DEFAULT false,
    derivation_id integer NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date, location_id, visit_n, event_type)
) PARTITION BY RANGE (service_date);

CREATE TABLE history.vehicle_assignment (
    feed text NOT NULL,
    source text NOT NULL,
    source_vehicle_id text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    method text NOT NULL,
    derivation_id integer NOT NULL,
    PRIMARY KEY (feed, source, source_vehicle_id, key_namespace, key, service_date)
) PARTITION BY RANGE (service_date);

-- Written by the nightly job (obehy jobs vehicle-day). tour_id and tour_match_share are
-- reserved for circulations (BASE_PLAN.md section 22) and stay null until then.
CREATE TABLE history.vehicle_day (
    feed text NOT NULL,
    source text NOT NULL,
    source_vehicle_id text NOT NULL,
    service_date date NOT NULL,
    seq integer NOT NULL,
    journeys jsonb NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    tour_id text,
    tour_match_share real,
    PRIMARY KEY (feed, source, source_vehicle_id, service_date, seq)
) PARTITION BY RANGE (service_date);
