-- The trip's calls, by which composition spans are told.
SELECT c.sequence, c.location_id, l.name
FROM active.trip_call c
JOIN active.location l ON l.location_id = c.location_id
WHERE c.trip_id = %(trip_id)s
ORDER BY c.sequence
