-- The sources whose vehicles history saw working journeys on a date, in distinct vehicles: only
-- those each source's policy calls physical for the journey's mode (as it ran).
SELECT a.source, count(DISTINCT a.source_vehicle_id) AS vehicles
FROM history.vehicle_assignment a
JOIN history.journey_latest s USING (feed, key_namespace, key, service_date)
WHERE a.service_date = %(service_date)s AND rt.is_physical(a.source, s.mode)
GROUP BY a.source
ORDER BY vehicles DESC, a.source
