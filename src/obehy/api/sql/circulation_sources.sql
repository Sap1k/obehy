-- The sources whose vehicles history saw working journeys on a date, in distinct vehicles: only
-- those each source's policy calls physical for the journey's mode (as it ran).
WITH physical AS (
    SELECT *
    FROM unnest(%(physical_sources)s::text[], %(physical_modes)s::text[],
        %(physical)s::boolean[]) AS p (source, mode, physical)
)
SELECT a.source, count(DISTINCT a.source_vehicle_id) AS vehicles
FROM history.vehicle_assignment a
JOIN history.journey j USING (feed, key_namespace, key, service_date)
JOIN history.journey_schedule s
    ON (s.feed, s.key_namespace, s.key, s.service_date, s.revision)
        = (j.feed, j.key_namespace, j.key, j.service_date, j.latest_revision)
LEFT JOIN physical pm ON pm.source = a.source AND pm.mode = s.mode
LEFT JOIN physical pd ON pd.source = a.source AND pd.mode = 'default'
WHERE a.service_date = %(service_date)s AND coalesce(pm.physical, pd.physical, true)
GROUP BY a.source
ORDER BY vehicles DESC, a.source
