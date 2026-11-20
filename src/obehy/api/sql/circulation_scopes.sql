-- The companies (line agencies) and modes a source's vehicles worked on a date: each recorded
-- journey's line as it ran (its latest schedule revision), in distinct vehicles per agency (mode
-- null) and per agency and mode. Only vehicles the source's policy calls physical for the mode.
WITH worked AS (
    SELECT DISTINCT a.source_vehicle_id, s.agency_id, s.mode
    FROM history.vehicle_assignment a
    JOIN history.journey_latest s USING (feed, key_namespace, key, service_date)
    WHERE a.source = %(source)s AND a.service_date = %(service_date)s
        AND s.agency_id IS NOT NULL AND rt.is_physical(a.source, s.mode)
)
SELECT w.agency_id, coalesce(ag.name, w.agency_id) AS name,
       CASE WHEN grouping(w.mode) = 0 THEN w.mode END AS mode,
       count(DISTINCT w.source_vehicle_id) AS vehicles
FROM worked w
LEFT JOIN active.agency ag ON ag.agency_id = w.agency_id
GROUP BY GROUPING SETS ((w.agency_id, ag.name), (w.agency_id, ag.name, w.mode))
ORDER BY w.agency_id, mode NULLS FIRST
