-- One vehicle's working days (BASE_PLAN.md section 19.3): its journeys in order of first
-- sighting, split where the gap between consecutive journeys exceeds %(gap_s)s seconds. A chain
-- is dated by its first journey's service date, so a day that crosses midnight stays whole.
-- Assignments of the neighbouring service dates are read so chains crossing a date boundary
-- are seen whole; only chains dated %(day)s are written.
WITH assignment AS (
    SELECT feed, source, source_vehicle_id, key_namespace, key, service_date, first_seen,
        last_seen
    FROM history.vehicle_assignment
    WHERE service_date BETWEEN %(day)s::date - 1 AND %(day)s::date + 1
),
marked AS (
    SELECT *,
        CASE
            WHEN lag(last_seen) OVER vehicle IS NULL THEN 1
            WHEN first_seen - lag(last_seen) OVER vehicle > make_interval(secs => %(gap_s)s)
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
INSERT INTO history.vehicle_day (feed, source, source_vehicle_id, service_date, seq, journeys,
    first_seen, last_seen)
SELECT feed, source, source_vehicle_id, service_date,
    row_number() OVER (PARTITION BY feed, source, source_vehicle_id ORDER BY first_seen),
    journeys, first_seen, last_seen
FROM chained
WHERE service_date = %(day)s::date
