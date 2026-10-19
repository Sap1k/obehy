-- The platforms, tracks and posts of a stop place.
SELECT location_id, name, public_code, latitude, longitude
FROM active.location
WHERE parent_location_id = %(location_id)s AND kind = 'boarding_point'
ORDER BY public_code NULLS LAST, location_id
