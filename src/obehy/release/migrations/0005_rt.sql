-- Realtime state (docs/R1_SLICE.md section 3). Every row carries release_id; readers join only
-- rows of the active publication. Daily partitions of rt.observation are created on demand by the
-- writer (obehy.realtime.emit.db) and dropped after the policy window.
CREATE SCHEMA rt;

CREATE TABLE rt.observation (
    received_at timestamptz NOT NULL,
    source text NOT NULL,
    channel text NOT NULL,
    feed text NOT NULL CHECK (feed IN ('jdf', 'czptt')),
    raw_sha256 text NOT NULL,
    raw_item integer NOT NULL,
    observed_at timestamptz,
    decoder_version integer NOT NULL,
    fact_schema_version integer NOT NULL,
    facts jsonb NOT NULL,
    journey_namespace text,
    journey_key text,
    service_date date,
    reason text,
    release_id text NOT NULL
) PARTITION BY RANGE (received_at);
CREATE INDEX observation_time ON rt.observation (received_at, source, channel);

-- Current state, rewritten every emit tick; UNLOGGED because a restart rebuilds it by warm replay.
CREATE UNLOGGED TABLE rt.vehicle_state_current (
    feed text NOT NULL,
    source text NOT NULL,
    source_vehicle_id text NOT NULL,
    status text NOT NULL,
    last_seen timestamptz NOT NULL,
    key_namespace text,
    key text,
    service_date date,
    trip_id text,
    latitude double precision,
    longitude double precision,
    bearing double precision,
    reason text,
    release_id text NOT NULL,
    PRIMARY KEY (feed, source, source_vehicle_id)
);

CREATE UNLOGGED TABLE rt.trip_state_current (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    release_id text NOT NULL,
    trip_id text NOT NULL,
    lifecycle text NOT NULL,
    delay_s integer,
    off_route boolean NOT NULL,
    stale boolean NOT NULL,
    updated_at timestamptz NOT NULL,
    calls jsonb NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date)
);

CREATE TABLE rt.source_health (
    source text NOT NULL,
    channel text NOT NULL,
    minute timestamptz NOT NULL,
    polls integer NOT NULL,
    errors integer NOT NULL,
    rows integer NOT NULL,
    lag_ms integer,
    PRIMARY KEY (source, channel, minute)
);
