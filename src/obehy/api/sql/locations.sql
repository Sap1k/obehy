-- Locations by ID: names and coordinates for line patterns.
SELECT location_id, name, latitude, longitude
FROM active.location
WHERE location_id = ANY(%(location_ids)s::text[])
