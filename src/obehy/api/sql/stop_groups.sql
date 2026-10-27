-- The groups of stop places named: a railway station with the stop in front of it (another
-- feed), one board for both. A station's partner is the nearest served surface stop place
-- within the radius whose name says it is the station's stop ("žel. st.", "hl. n.", "Bahnhof"),
-- if one of the few nearest does, else the nearest. A group is a stop with every station whose
-- partner it is, so it is the same from either side. Every station near enough to matter (twice
-- the radius: a station, its partner, the partner's other stations) gets its partner once. Names
-- match a word when their folded letters and digits, split on everything else, hold the word's.
WITH asked AS (
    SELECT x.location_id AS asked, l.domain, l.geom
    FROM unnest(%(location_ids)s::text[]) AS x (location_id)
    JOIN active.location l ON l.location_id = x.location_id AND l.kind = 'stop_place'
    WHERE l.geom IS NOT NULL
),
stations AS (
    SELECT DISTINCT s.location_id, s.geom
    FROM asked a
    JOIN active.location s
        ON s.kind = 'stop_place' AND s.domain = 'heavy_rail'
        AND s.geom && ST_Expand(a.geom, 2 * %(degrees)s)
        AND ST_DWithin(s.geom::geography, a.geom::geography, 2 * %(radius)s)
    WHERE EXISTS (SELECT 1 FROM active.location_mode m WHERE m.location_id = s.location_id)
),
partners AS (
    SELECT s.location_id AS station, p.location_id AS stop
    FROM stations s
    CROSS JOIN LATERAL (
        SELECT c.location_id
        FROM (
            SELECT n.location_id, n.named,
                   row_number() OVER (ORDER BY n.distance, n.location_id) AS rank
            FROM (
                SELECT l.location_id,
                       ST_Distance(l.geom::geography, s.geom::geography) AS distance,
                       EXISTS (
                           SELECT 1 FROM unnest(%(station_words)s::text[]) AS w
                           WHERE strpos(
                               ' ' || regexp_replace(control.obehy_fold(l.name), '[^a-z0-9]+',
                                                     ' ', 'g') || ' ',
                               ' ' || w || ' ') > 0
                       ) AS named
                FROM active.location l
                WHERE l.kind = 'stop_place' AND l.domain = 'surface'
                    AND l.geom && ST_Expand(s.geom, %(degrees)s)
                    AND ST_DWithin(l.geom::geography, s.geom::geography, %(radius)s)
                    AND EXISTS (
                        SELECT 1 FROM active.location_mode m WHERE m.location_id = l.location_id
                    )
            ) n
        ) c
        WHERE c.rank <= %(candidates)s
        ORDER BY c.named DESC, c.rank
        LIMIT 1
    ) p
),
hubs AS (
    SELECT a.asked, CASE WHEN a.domain = 'surface' THEN a.asked ELSE p.stop END AS hub
    FROM asked a
    LEFT JOIN partners p ON p.station = a.asked
),
members AS (
    SELECT h.asked, h.hub AS location_id FROM hubs h WHERE h.hub IS NOT NULL
    UNION
    SELECT h.asked, p.station FROM hubs h JOIN partners p ON p.stop = h.hub
)
SELECT m.asked, l.location_id, l.name, l.kind, l.domain, l.municipality_name AS municipality,
       l.district_name AS district, l.latitude, l.longitude, l.coordinate_precision,
       ARRAY(SELECT o.mode FROM active.location_mode o WHERE o.location_id = l.location_id)
           AS modes
FROM members m
JOIN active.location l ON l.location_id = m.location_id
WHERE (SELECT count(*) FROM members o WHERE o.asked = m.asked) > 1
ORDER BY m.asked, l.domain = 'surface', l.name, l.location_id
