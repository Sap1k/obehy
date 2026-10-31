-- The companies (line agencies) and modes a source's vehicles worked on a date: each recorded
-- journey's line as it ran (its latest schedule revision), in distinct vehicles per agency (mode
-- null) and per agency and mode. Only vehicles the source's policy calls physical for the mode.
WITH physical AS (
    SELECT *
    FROM unnest(%(physical_sources)s::text[], %(physical_modes)s::text[],
        %(physical)s::boolean[]) AS p (source, mode, physical)
),
worked AS (
    SELECT DISTINCT a.source_vehicle_id, s.agency_id, s.mode
    FROM history.vehicle_assignment a
    JOIN history.journey j USING (feed, key_namespace, key, service_date)
    JOIN history.journey_schedule s
        ON (s.feed, s.key_namespace, s.key, s.service_date, s.revision)
            = (j.feed, j.key_namespace, j.key, j.service_date, j.latest_revision)
    LEFT JOIN physical pm ON pm.source = a.source AND pm.mode = s.mode
    LEFT JOIN physical pd ON pd.source = a.source AND pd.mode = 'default'
    WHERE a.source = %(source)s AND a.service_date = %(service_date)s
        AND s.agency_id IS NOT NULL AND coalesce(pm.physical, pd.physical, true)
)
SELECT w.agency_id, coalesce(ag.name, w.agency_id) AS name,
       CASE WHEN grouping(w.mode) = 0 THEN w.mode END AS mode,
       count(DISTINCT w.source_vehicle_id) AS vehicles
FROM worked w
LEFT JOIN active.agency ag ON ag.agency_id = w.agency_id
GROUP BY GROUPING SETS ((w.agency_id, ag.name), (w.agency_id, ag.name, w.mode))
ORDER BY w.agency_id, mode NULLS FIRST
