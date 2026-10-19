-- The journey a trip ID of an earlier release ran as on the date (history keeps it).
SELECT key_namespace, key
FROM history.journey_schedule
WHERE trip_id = %(trip_id)s AND service_date = %(service_date)s
ORDER BY revision DESC
LIMIT 1
