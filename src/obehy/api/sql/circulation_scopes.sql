-- The companies (line agencies) and modes a source's vehicles worked on a date: each recorded
-- journey resolved by its key to the active release's trip valid that date, in distinct
-- vehicles per agency (mode null) and per agency and mode.
WITH worked AS (
    SELECT DISTINCT a.source_vehicle_id, r.agency_id, r.mode
    FROM history.vehicle_assignment a
    JOIN active.source_key k
        ON k.entity_kind = 'trip' AND (k.namespace, k.identifier) = (a.key_namespace, a.key)
        AND a.service_date BETWEEN k.valid_from AND k.valid_to
    JOIN active.trip t ON t.trip_id = k.public_id
    JOIN active.route r ON r.route_id = t.route_id
    WHERE a.source = %(source)s AND a.service_date = %(service_date)s
        AND r.agency_id IS NOT NULL
)
SELECT w.agency_id, ag.name, CASE WHEN grouping(w.mode) = 0 THEN w.mode END AS mode,
       count(DISTINCT w.source_vehicle_id) AS vehicles
FROM worked w
JOIN active.agency ag ON ag.agency_id = w.agency_id
GROUP BY GROUPING SETS ((w.agency_id, ag.name), (w.agency_id, ag.name, w.mode))
ORDER BY w.agency_id, mode NULLS FIRST
