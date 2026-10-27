-- Stop places within a radius of a point, nearest first (the GiST index finds the box).
WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS point)
SELECT l.location_id, l.name, l.kind, l.domain, l.municipality_name AS municipality,
       l.district_name AS district, l.latitude, l.longitude, l.coordinate_precision,
       ST_Distance(l.geom::geography, p.point::geography) AS distance_m,
       ARRAY(SELECT m.mode FROM active.location_mode m WHERE m.location_id = l.location_id)
           AS modes
FROM active.location l, p
WHERE l.kind = 'stop_place'
    AND l.geom && ST_Expand(p.point, %(degrees)s)
    AND ST_DWithin(l.geom::geography, p.point::geography, %(radius)s)
ORDER BY distance_m, l.location_id
LIMIT %(limit)s
