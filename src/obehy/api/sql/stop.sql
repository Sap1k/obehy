-- A stop place by its ID, or the stop place of a boarding point. Railway operational points are
-- not public stops.
SELECT p.location_id, p.name, p.kind, p.domain, p.municipality_name AS municipality,
       p.district_name AS district, p.latitude, p.longitude, p.coordinate_precision,
       p.wheelchair_boarding
FROM active.location l
JOIN active.location p ON p.location_id = coalesce(l.parent_location_id, l.location_id)
WHERE l.location_id = %(location_id)s AND l.kind <> 'operational_point'
    AND p.kind = 'stop_place'
