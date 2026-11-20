-- One vehicle identity, one physical-vehicle policy, one latest schedule (BASE_PLAN.md sections
-- 19.3, 22 and 27).

-- Which vehicle a source's report on a journey was, as the worker resolved it when it wrote the
-- assignment (emit/vehicles.py: a registry key, an identifier the source states, a confirmed
-- co-located partner, else `<source>:<source_vehicle_id>`), so history, the live map and a
-- vehicle's page name a vehicle alike. The days kept so far get what history.vehicle_of said:
-- the registry's source-ID key valid on the date (the highest-priority dataset's), else the
-- source form.
ALTER TABLE history.vehicle_assignment ADD COLUMN vehicle_id text;

WITH reports AS (
    SELECT DISTINCT source, source_vehicle_id, service_date
    FROM history.vehicle_assignment
),
keyed AS (
    SELECT DISTINCT ON (r.source, r.source_vehicle_id, r.service_date)
        r.source, r.source_vehicle_id, r.service_date, k.vehicle_id
    FROM reports r
    JOIN ref.vehicle_key k
        ON k.source = r.source AND k.source_vehicle_id = r.source_vehicle_id
        AND r.service_date >= k.valid_from
        AND (k.valid_to IS NULL OR r.service_date <= k.valid_to)
    JOIN ref.dataset d ON d.dataset = k.dataset
    ORDER BY r.source, r.source_vehicle_id, r.service_date, d.priority, k.vehicle_id
)
UPDATE history.vehicle_assignment a
SET vehicle_id = coalesce(k.vehicle_id, a.source || ':' || a.source_vehicle_id)
FROM reports r
LEFT JOIN keyed k USING (source, source_vehicle_id, service_date)
WHERE (r.source, r.source_vehicle_id, r.service_date)
    = (a.source, a.source_vehicle_id, a.service_date);

ALTER TABLE history.vehicle_assignment ALTER COLUMN vehicle_id SET NOT NULL;
CREATE INDEX vehicle_assignment_vehicle_id ON history.vehicle_assignment
    USING btree (vehicle_id, service_date);

DROP FUNCTION history.vehicle_chains(date, integer, text[], text[]);
DROP FUNCTION history.vehicle_of(text, text, date);

-- One vehicle's working days (BASE_PLAN.md section 19.3): the journeys its reports worked (a
-- journey two sources saw it on counted once, from the first sighting to the last), in order of
-- first sighting, split where the gap between consecutive journeys exceeds `gap_s` seconds. A
-- chain is dated by its first journey's service date, so a day that crosses midnight stays
-- whole, and names the report that saw its first journey first. Assignments of the
-- neighbouring service dates are read so chains crossing a date boundary are seen whole; only
-- chains dated `day` are returned. `sources` and `source_vehicle_ids` (pairwise) narrow it to
-- some reports, `vehicle` to one vehicle; null reads them all. Inlined into the caller.
CREATE FUNCTION history.vehicle_chains(
    day date,
    gap_s integer,
    sources text[] DEFAULT NULL,
    source_vehicle_ids text[] DEFAULT NULL,
    vehicle text DEFAULT NULL
)
RETURNS TABLE (
    feed text,
    vehicle_id text,
    source text,
    source_vehicle_id text,
    service_date date,
    seq integer,
    journeys jsonb,
    first_seen timestamptz,
    last_seen timestamptz
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
WITH assignment AS (
    SELECT a.feed, a.vehicle_id, a.key_namespace, a.key, a.service_date,
        min(a.first_seen) AS first_seen, max(a.last_seen) AS last_seen,
        (array_agg(a.source ORDER BY a.first_seen, a.source, a.source_vehicle_id))[1] AS source,
        (array_agg(a.source_vehicle_id ORDER BY a.first_seen, a.source, a.source_vehicle_id))[1]
            AS source_vehicle_id
    FROM history.vehicle_assignment a
    WHERE a.service_date BETWEEN day - 1 AND day + 1
        AND (vehicle IS NULL OR a.vehicle_id = vehicle)
        AND (
            sources IS NULL
            OR (a.source, a.source_vehicle_id) IN (
                SELECT * FROM unnest(sources, source_vehicle_ids)
            )
        )
    GROUP BY a.feed, a.vehicle_id, a.key_namespace, a.key, a.service_date
),
marked AS (
    SELECT *,
        CASE
            WHEN lag(last_seen) OVER vehicle IS NULL THEN 1
            WHEN first_seen - lag(last_seen) OVER vehicle > make_interval(secs => gap_s)
                THEN 1
            ELSE 0
        END AS starts
    FROM assignment
    WINDOW vehicle AS (
        PARTITION BY feed, vehicle_id ORDER BY first_seen, key_namespace, key, service_date
    )
),
numbered AS (
    SELECT *,
        sum(starts) OVER (
            PARTITION BY feed, vehicle_id ORDER BY first_seen, key_namespace, key, service_date
        ) AS chain
    FROM marked
),
chained AS (
    SELECT feed, vehicle_id, chain,
        (array_agg(source ORDER BY first_seen, key_namespace, key))[1] AS source,
        (array_agg(source_vehicle_id ORDER BY first_seen, key_namespace, key))[1]
            AS source_vehicle_id,
        (array_agg(service_date ORDER BY first_seen, key_namespace, key))[1] AS service_date,
        jsonb_agg(
            jsonb_build_object(
                'key_namespace', key_namespace, 'key', key, 'service_date', service_date,
                'first_seen', first_seen, 'last_seen', last_seen
            )
            ORDER BY first_seen, key_namespace, key
        ) AS journeys,
        min(first_seen) AS first_seen,
        max(last_seen) AS last_seen
    FROM numbered
    GROUP BY feed, vehicle_id, chain
)
SELECT feed, vehicle_id, source, source_vehicle_id, service_date,
    (row_number() OVER (PARTITION BY feed, vehicle_id ORDER BY first_seen))::integer,
    journeys, first_seen, last_seen
FROM chained
WHERE service_date = day
$$;

-- Never read: a vehicle's days are cut live by history.vehicle_chains, today's included.
DROP TABLE history.vehicle_day;

-- Whether a source's vehicle IDs on a mode's journeys name physical vehicles (its manifest's
-- `[vehicles] physical`; mode `default` for its other modes): a metro run or a train keyed by its
-- number is no vehicle to follow through a day. Written by the worker from the manifests at
-- start (emit/vehicles.py); read through rt.is_physical.
CREATE TABLE rt.physical_vehicles (
    source text NOT NULL,
    mode text NOT NULL,
    physical boolean NOT NULL,
    PRIMARY KEY (source, mode)
);

-- A source's vehicle IDs on `mode`'s journeys (null: unknown) are physical: the mode's entry,
-- else the source's default, else true (a source that declares nothing).
CREATE FUNCTION rt.is_physical(source text, mode text)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL SAFE
RETURN coalesce(
    (
        SELECT p.physical
        FROM rt.physical_vehicles p
        WHERE p.source = is_physical.source AND p.mode IN (is_physical.mode, 'default')
        ORDER BY p.mode = 'default'
        LIMIT 1
    ),
    true
);

-- Each journey with its latest schedule snapshot: the journey as history knows it now (trip ID,
-- line as it ran, headsign).
CREATE VIEW history.journey_latest AS
SELECT j.journey_id, j.feed, j.key_namespace, j.key, j.service_date, j.latest_revision,
    s.trip_id, s.route_name, s.headsign, s.route_id, s.mode, s.agency_id
FROM history.journey j
JOIN history.journey_schedule s
    ON (s.feed, s.key_namespace, s.key, s.service_date, s.revision)
        = (j.feed, j.key_namespace, j.key, j.service_date, j.latest_revision);

-- A unit is on a trip when it leaves one of the trip's calls on it: a formation answered from
-- the trip's last call (the next part of the run, which shares the train number) is the next
-- part's, not this one's.
CREATE OR REPLACE FUNCTION rt.composition_spans(
    for_trip text, for_feed text, for_namespace text, for_key text, for_date date
)
RETURNS TABLE (
    composition_id bigint, source text, observed_at timestamptz, unit_sequence integer,
    kind text, role text, vehicle_id text, vehicle_type text, coach_number text,
    classes integer[], features text[], texts jsonb, destination text, details jsonb,
    from_sequence integer, to_sequence integer, open boolean
)
LANGUAGE sql STABLE AS $$
    WITH calls AS (
        SELECT c.sequence, c.location_id,
               row_number() OVER (PARTITION BY c.location_id ORDER BY c.sequence)::integer
                   AS visit_n
        FROM active.trip_call c
        WHERE c.trip_id = for_trip
    )
    SELECT m.composition_id, m.source, m.observed_at, u.sequence, u.kind, u.role, u.vehicle_id,
           u.vehicle_type, u.coach_number, u.classes, u.features, u.texts, u.destination,
           u.details, f.sequence, coalesce(t.sequence, (SELECT max(sequence) FROM calls)),
           coalesce(u.to_location, m.to_location) IS NULL
    FROM rt.composition m
    JOIN rt.composition_unit u ON u.composition_id = m.composition_id
    JOIN calls f
        ON f.location_id = coalesce(u.from_location, m.from_location)
        AND f.visit_n = coalesce(u.from_visit, m.from_visit)
    LEFT JOIN calls t
        ON t.location_id = coalesce(u.to_location, m.to_location)
        AND t.visit_n = coalesce(u.to_visit, m.to_visit)
    WHERE m.feed = for_feed AND m.key_namespace = for_namespace AND m.key = for_key
        AND m.service_date = for_date
        AND f.sequence < (SELECT max(sequence) FROM calls)
        AND (coalesce(u.to_location, m.to_location) IS NULL OR t.sequence IS NOT NULL)
$$;
