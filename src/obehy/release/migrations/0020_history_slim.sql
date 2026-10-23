-- Slim, daily history (BASE_PLAN.md section 29). History grew by about 1.15 GB a day with PID,
-- almost all of it stop events and scheduled calls repeating the journey's text key and the stop's
-- ID and name on every row and in every primary key. Now:
-- - history.journey numbers each journey (journey_id, from a sequence); its natural key stays the
--   primary key and the journey's identity;
-- - journey_call and actual_stop_event refer to the journey by that number and to stop IDs, stop
--   names, methods and sources through history.term, a dictionary of the strings history keeps
--   (self-described: nothing references static.*); history.named_call and
--   history.named_stop_event read them back as text;
-- - every history table is partitioned by service date per day, so the nightly job drops the days
--   past `[retention] history_days` whole.
-- The rows already recorded are moved over. This runs once, for some minutes on a server with a
-- month of PID.

CREATE FUNCTION pg_temp.day_partitions(parent text, lo date, hi date) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    d date;
BEGIN
    IF lo IS NULL THEN
        RETURN;
    END IF;
    FOR d IN SELECT generate_series(lo, hi, interval '1 day')::date LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS history.%I PARTITION OF history.%I FOR VALUES FROM (%L) TO (%L)',
            parent || '_' || to_char(d, 'YYYYMMDD'), parent, d, d + 1);
    END LOOP;
END $$;

-- Moves a history table's partitions (monthly until now) to daily ones; `selected` is what is
-- inserted for the parent's columns (all of them, in order, by default).
CREATE FUNCTION pg_temp.to_daily(parent text, selected text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    parts regclass[];
    part regclass;
    columns text;
    lo date;
    hi date;
BEGIN
    SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO columns
    FROM pg_attribute
    WHERE attrelid = format('history.%I', parent)::regclass AND attnum > 0 AND NOT attisdropped;
    SELECT coalesce(array_agg(i.inhrelid::regclass), '{}') INTO parts
    FROM pg_inherits i WHERE i.inhparent = format('history.%I', parent)::regclass;
    FOREACH part IN ARRAY parts LOOP
        EXECUTE format('ALTER TABLE history.%I DETACH PARTITION %s', parent, part);
        EXECUTE format('SELECT min(service_date), max(service_date) FROM %s', part) INTO lo, hi;
        PERFORM pg_temp.day_partitions(parent, lo, hi);
        EXECUTE format('INSERT INTO history.%I (%s) SELECT %s FROM %s',
            parent, columns, coalesce(selected, columns), part);
        EXECUTE format('DROP TABLE %s', part);
    END LOOP;
END $$;

-- The two large tables are rebuilt from their old rows.
CREATE SCHEMA history_old;
ALTER TABLE history.journey_call SET SCHEMA history_old;
ALTER TABLE history.actual_stop_event SET SCHEMA history_old;

-- Journeys get their number.
CREATE SEQUENCE history.journey_id_seq AS bigint;
ALTER TABLE history.journey ADD COLUMN journey_id bigint;
SELECT pg_temp.to_daily('journey',
    'feed, key_namespace, key, service_date, first_bound_at, latest_revision,'
    ' nextval(''history.journey_id_seq'')');
ALTER TABLE history.journey
    ALTER COLUMN journey_id SET DEFAULT nextval('history.journey_id_seq'),
    ALTER COLUMN journey_id SET NOT NULL,
    ADD UNIQUE (journey_id, service_date);
ALTER SEQUENCE history.journey_id_seq OWNED BY history.journey.journey_id;

-- The strings history keeps: stop IDs, stop names, methods and sources. Never pruned; it grows
-- only with distinct values.
CREATE TABLE history.term (
    term_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    value text NOT NULL UNIQUE
);

CREATE TYPE history.event_kind AS ENUM ('arrival', 'departure', 'passage');

-- Columns widest first, so the rows pack without padding.
CREATE TABLE history.journey_call (
    journey_id bigint NOT NULL,
    service_date date NOT NULL,
    location_term integer NOT NULL,
    name_term integer NOT NULL,
    scheduled_arrival integer,
    scheduled_departure integer,
    revision smallint NOT NULL,
    ordinal smallint NOT NULL,
    visit_n smallint NOT NULL,
    passenger_service boolean NOT NULL
) PARTITION BY RANGE (service_date);

-- Events attach to (location, visit_n), never to an ordinal. An event whose call vanished in a
-- later revision keeps its revision and is flagged orphaned; it is never moved or deleted.
CREATE TABLE history.actual_stop_event (
    journey_id bigint NOT NULL,
    event_time timestamptz NOT NULL,
    interval_lo timestamptz NOT NULL,
    interval_hi timestamptz NOT NULL,
    service_date date NOT NULL,
    location_term integer NOT NULL,
    method_term integer NOT NULL,
    source_term integer NOT NULL,
    derivation_id integer NOT NULL,
    revision smallint NOT NULL,
    visit_n smallint NOT NULL,
    event_type history.event_kind NOT NULL,
    orphaned boolean NOT NULL DEFAULT false
) PARTITION BY RANGE (service_date);

INSERT INTO history.term (value)
SELECT location_id FROM history_old.journey_call
UNION SELECT name FROM history_old.journey_call
UNION SELECT location_id FROM history_old.actual_stop_event
UNION SELECT method FROM history_old.actual_stop_event
UNION SELECT source FROM history_old.actual_stop_event
ORDER BY 1;

SELECT pg_temp.day_partitions('journey_call', min(service_date), max(service_date))
FROM history_old.journey_call;
SELECT pg_temp.day_partitions('actual_stop_event', min(service_date), max(service_date))
FROM history_old.actual_stop_event;

INSERT INTO history.journey_call (journey_id, service_date, location_term, name_term,
    scheduled_arrival, scheduled_departure, revision, ordinal, visit_n, passenger_service)
SELECT j.journey_id, c.service_date, l.term_id, n.term_id, c.scheduled_arrival,
    c.scheduled_departure, c.revision, c.ordinal, c.visit_n, c.passenger_service
FROM history_old.journey_call c
JOIN history.journey j
    ON (j.feed, j.key_namespace, j.key, j.service_date)
        = (c.feed, c.key_namespace, c.key, c.service_date)
JOIN history.term l ON l.value = c.location_id
JOIN history.term n ON n.value = c.name;

INSERT INTO history.actual_stop_event (journey_id, event_time, interval_lo, interval_hi,
    service_date, location_term, method_term, source_term, derivation_id, revision, visit_n,
    event_type, orphaned)
SELECT j.journey_id, e.event_time, e.interval_lo, e.interval_hi, e.service_date, l.term_id,
    m.term_id, s.term_id, e.derivation_id, e.revision, e.visit_n,
    e.event_type::history.event_kind, e.orphaned
FROM history_old.actual_stop_event e
JOIN history.journey j
    ON (j.feed, j.key_namespace, j.key, j.service_date)
        = (e.feed, e.key_namespace, e.key, e.service_date)
JOIN history.term l ON l.value = e.location_id
JOIN history.term m ON m.value = e.method
JOIN history.term s ON s.value = e.source;

-- Built after the load: a sorted build packs the indexes full.
ALTER TABLE history.journey_call
    ADD PRIMARY KEY (journey_id, service_date, revision, ordinal);
ALTER TABLE history.actual_stop_event
    ADD PRIMARY KEY (journey_id, service_date, location_term, visit_n, event_type);

DROP SCHEMA history_old CASCADE;

SELECT pg_temp.to_daily('journey_schedule');
SELECT pg_temp.to_daily('vehicle_assignment');
SELECT pg_temp.to_daily('vehicle_day');
SELECT pg_temp.to_daily('journey_link');
SELECT pg_temp.to_daily('platform_evidence');
SELECT pg_temp.to_daily('journey_cancellation');

-- The slim tables read back as text, keyed by journey as before (for tests and for looking).
CREATE VIEW history.named_call AS
SELECT j.feed, j.key_namespace, j.key, c.service_date, c.revision, c.ordinal,
    l.value AS location_id, c.visit_n, c.passenger_service, c.scheduled_arrival,
    c.scheduled_departure, n.value AS name
FROM history.journey_call c
JOIN history.journey j USING (journey_id, service_date)
JOIN history.term l ON l.term_id = c.location_term
JOIN history.term n ON n.term_id = c.name_term;

CREATE VIEW history.named_stop_event AS
SELECT j.feed, j.key_namespace, j.key, e.service_date, l.value AS location_id, e.visit_n,
    e.event_type::text AS event_type, e.revision, e.event_time, e.interval_lo, e.interval_hi,
    m.value AS method, s.value AS source, e.orphaned, e.derivation_id
FROM history.actual_stop_event e
JOIN history.journey j USING (journey_id, service_date)
JOIN history.term l ON l.term_id = e.location_term
JOIN history.term m ON m.term_id = e.method_term
JOIN history.term s ON s.term_id = e.source_term;
