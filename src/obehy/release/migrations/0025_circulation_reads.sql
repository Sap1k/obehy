-- Observed circulations (BASE_PLAN.md section 22) for the API as well as the nightly job: the
-- chain rule as one function both read, which registry vehicle a source vehicle was, and the
-- indexes that find a vehicle's or a journey's assignments and a trip's static block.

-- The vehicle a source's vehicle was on a service date: the registry's by a source-ID key
-- (the best-ranked dataset first), else its own `<source>:<source_vehicle_id>`. The worker also
-- binds by licence plate, co-location and train; those bindings are live only, so a vehicle
-- recognised only that way has no history under its registry ID.
CREATE FUNCTION history.vehicle_of(source text, source_vehicle_id text, day date)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
RETURN coalesce(
    (
        SELECT k.vehicle_id
        FROM ref.vehicle_key k
        JOIN ref.dataset d ON d.dataset = k.dataset
        WHERE k.source = vehicle_of.source AND k.source_vehicle_id = vehicle_of.source_vehicle_id
            AND vehicle_of.day >= k.valid_from
            AND (k.valid_to IS NULL OR vehicle_of.day <= k.valid_to)
        ORDER BY d.priority, k.vehicle_id
        LIMIT 1
    ),
    vehicle_of.source || ':' || vehicle_of.source_vehicle_id
);

-- One vehicle's working days (BASE_PLAN.md section 19.3): its journeys in order of first
-- sighting, split where the gap between consecutive journeys exceeds `gap_s` seconds. A chain
-- is dated by its first journey's service date, so a day that crosses midnight stays whole.
-- Assignments of the neighbouring service dates are read so chains crossing a date boundary
-- are seen whole; only chains dated `day` are returned. `sources` and `source_vehicle_ids`
-- (pairwise) narrow it to some vehicles; null reads them all. Inlined into the caller.
CREATE FUNCTION history.vehicle_chains(
    day date,
    gap_s integer,
    sources text[] DEFAULT NULL,
    source_vehicle_ids text[] DEFAULT NULL
)
RETURNS TABLE (
    feed text,
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
    SELECT a.feed, a.source, a.source_vehicle_id, a.key_namespace, a.key, a.service_date,
        a.first_seen, a.last_seen
    FROM history.vehicle_assignment a
    WHERE a.service_date BETWEEN day - 1 AND day + 1
        AND (
            sources IS NULL
            OR (a.source, a.source_vehicle_id) IN (
                SELECT * FROM unnest(sources, source_vehicle_ids)
            )
        )
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
        PARTITION BY feed, source, source_vehicle_id
        ORDER BY first_seen, key_namespace, key, service_date
    )
),
numbered AS (
    SELECT *,
        sum(starts) OVER (
            PARTITION BY feed, source, source_vehicle_id
            ORDER BY first_seen, key_namespace, key, service_date
        ) AS chain
    FROM marked
),
chained AS (
    SELECT feed, source, source_vehicle_id, chain,
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
    GROUP BY feed, source, source_vehicle_id, chain
)
SELECT feed, source, source_vehicle_id, service_date,
    (row_number() OVER (PARTITION BY feed, source, source_vehicle_id ORDER BY first_seen))::integer,
    journeys, first_seen, last_seen
FROM chained
WHERE service_date = day
$$;

CREATE INDEX vehicle_assignment_vehicle ON history.vehicle_assignment
    USING btree (source, source_vehicle_id, service_date);
CREATE INDEX vehicle_assignment_journey ON history.vehicle_assignment
    USING btree (key_namespace, key, service_date);
CREATE INDEX trip_block ON static.trip USING btree (load_id, block_key);
