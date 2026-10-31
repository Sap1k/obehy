-- A source's journeys on a date in a scope: of the given agencies (and modes, when given), of
-- one route, or of one vehicle, by the line each journey ran as (its latest schedule revision).
-- The vehicles that worked them, those the source's policy calls physical, are the scope's rows.
WITH physical AS (
    SELECT *
    FROM unnest(%(physical_sources)s::text[], %(physical_modes)s::text[],
        %(physical)s::boolean[]) AS p (source, mode, physical)
)
SELECT DISTINCT a.source_vehicle_id, a.key_namespace, a.key
FROM history.vehicle_assignment a
JOIN history.journey j USING (feed, key_namespace, key, service_date)
JOIN history.journey_schedule s
    ON (s.feed, s.key_namespace, s.key, s.service_date, s.revision)
        = (j.feed, j.key_namespace, j.key, j.service_date, j.latest_revision)
LEFT JOIN physical pm ON pm.source = a.source AND pm.mode = s.mode
LEFT JOIN physical pd ON pd.source = a.source AND pd.mode = 'default'
WHERE a.source = %(source)s AND a.service_date = %(service_date)s
    AND coalesce(pm.physical, pd.physical, true)
    AND CASE
        WHEN %(source_vehicle_id)s::text IS NOT NULL
            THEN a.source_vehicle_id = %(source_vehicle_id)s
        WHEN %(route_id)s::text IS NOT NULL THEN s.route_id = %(route_id)s
        ELSE s.agency_id = ANY(%(agency_ids)s::text[])
            AND (%(modes)s::text[] IS NULL OR s.mode = ANY(%(modes)s::text[]))
    END
ORDER BY a.source_vehicle_id, a.key_namespace, a.key
