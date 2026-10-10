-- Rail runs, journey links and platforms (docs/R2_SLICE.md section 9). Monthly partitions by
-- service_date are created on demand by the writer, like the other history tables.

-- A train that continues under a new number is two journeys of one run (BASE_PLAN.md 9.3).
CREATE TABLE history.journey_link (
    feed text NOT NULL,
    source_namespace text NOT NULL,
    source_key text NOT NULL,
    target_namespace text NOT NULL,
    target_key text NOT NULL,
    service_date date NOT NULL,
    kind text NOT NULL CHECK (kind IN ('continues_as', 'splits_from', 'joins')),
    derivation_id integer NOT NULL,
    PRIMARY KEY (feed, source_namespace, source_key, target_namespace, target_key, service_date)
) PARTITION BY RANGE (service_date);

-- Platforms and tracks sources assigned to calls (BASE_PLAN.md section 24). value is what is
-- shown; boarding_point_id only where a track maps to a static boarding point.
CREATE TABLE history.platform_evidence (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    location_id text NOT NULL,
    visit_n integer NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('arrival', 'departure')),
    value text NOT NULL,
    label text NOT NULL CHECK (label IN ('platform', 'track')),
    boarding_point_id text,
    source text NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    derivation_id integer NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date, location_id, visit_n, event_type, value)
) PARTITION BY RANGE (service_date);

-- The CZPTT path (PA) a rail journey runs on.
ALTER TABLE history.journey_schedule ADD COLUMN run_key text;
