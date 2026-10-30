-- The group of a stop place (a railway station and the stop in front of it,
-- active.stop_group): its members as stops, stations first; none when it stands alone.
WITH asked AS (
    SELECT g.group_id FROM active.stop_group g WHERE g.location_id = %(location_id)s
),
members AS (
    SELECT g.location_id, g.station
    FROM active.stop_group g
    JOIN asked a ON a.group_id = g.group_id
)
SELECT l.location_id, l.name, l.kind, l.domain, l.municipality_name AS municipality,
       l.district_name AS district, l.latitude, l.longitude, l.coordinate_precision,
       ARRAY(SELECT m.mode FROM active.location_mode m WHERE m.location_id = l.location_id)
           AS modes
FROM members g
JOIN active.location l ON l.location_id = g.location_id
ORDER BY g.station DESC, l.name, l.location_id
