-- Paths as GeoJSON geometries, by shape ID.
SELECT shape_id, ST_AsGeoJSON(geom)::jsonb AS geometry
FROM active.shape_line
WHERE shape_id = ANY(%(shape_ids)s::text[])
