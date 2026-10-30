-- The sources whose vehicles history saw working journeys on a date, in distinct vehicles.
SELECT source, count(DISTINCT source_vehicle_id) AS vehicles
FROM history.vehicle_assignment
WHERE service_date = %(service_date)s
GROUP BY source
ORDER BY vehicles DESC, source
