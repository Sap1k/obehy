-- A trip's path as a GeoJSON geometry.
SELECT ST_AsGeoJSON(geom)::jsonb AS geometry
FROM active.shape_line
WHERE shape_id = %(shape_id)s
