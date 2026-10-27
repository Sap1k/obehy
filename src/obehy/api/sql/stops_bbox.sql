-- Stop places with coordinates inside a viewport.
SELECT l.location_id, l.name, l.kind, l.domain, l.municipality_name AS municipality,
       l.district_name AS district, l.latitude, l.longitude, l.coordinate_precision,
       ARRAY(SELECT m.mode FROM active.location_mode m WHERE m.location_id = l.location_id)
           AS modes
FROM active.location l
WHERE l.kind = 'stop_place'
    AND l.geom && ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, 4326)
ORDER BY l.location_id
LIMIT %(limit)s
