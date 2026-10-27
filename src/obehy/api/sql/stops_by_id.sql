-- Stop places by ID, in the order given (a boarding point names its stop place); railway
-- operational points are not public stops.
SELECT x.location_id AS asked, p.location_id, p.name, p.kind, p.domain,
       p.municipality_name AS municipality, p.district_name AS district, p.latitude,
       p.longitude, p.coordinate_precision, p.wheelchair_boarding,
       ARRAY(SELECT m.mode FROM active.location_mode m WHERE m.location_id = p.location_id)
           AS modes
FROM unnest(%(location_ids)s::text[]) WITH ORDINALITY AS x (location_id, n)
JOIN active.location l ON l.location_id = x.location_id AND l.kind <> 'operational_point'
JOIN active.location p
    ON p.location_id = coalesce(l.parent_location_id, l.location_id) AND p.kind = 'stop_place'
ORDER BY x.n
