-- Pair every served railway station of a load pair with a surface stop (BASE_PLAN.md section
-- 7.4; release/pairing.py runs it after clearing the pair's rows). The candidates are the
-- served surface stop places within the radius, nearest first, `candidates` of them, less those
-- an `apart` override keeps from the station. The named one wins (its name, folded and split on
-- everything but letters and digits, holds a station word), else the nearest; two named ones,
-- or with none named the nearest two, within `margin` of each other's distance are a tie, left
-- `ambiguous`. A `pair` override naming a stop of the release decides; an `apart` override
-- without a stop keeps the station alone. The rule's own choice is kept beside the override's.
WITH overrides AS (
    SELECT station_id, stop_id, decision FROM ref.station_pair
),
stations AS (
    SELECT l.location_id, l.geom
    FROM static.location l
    WHERE l.load_id = %(czptt)s AND l.kind = 'stop_place' AND l.domain = 'heavy_rail'
        AND l.geom IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM static.location_mode m
            WHERE m.load_id = l.load_id AND m.location_id = l.location_id
        )
),
ranked AS (
    SELECT s.location_id AS station_id, c.location_id AS stop_id, c.name, c.distance,
           EXISTS (
               SELECT 1 FROM unnest(%(station_words)s::text[]) AS w
               WHERE strpos(
                   ' ' || regexp_replace(control.obehy_fold(c.name), '[^a-z0-9]+', ' ', 'g')
                       || ' ',
                   ' ' || w || ' ') > 0
           ) AS named,
           row_number() OVER (PARTITION BY s.location_id ORDER BY c.distance, c.location_id)
               AS rank
    FROM stations s
    CROSS JOIN LATERAL (
        SELECT l.location_id, l.name, ST_Distance(l.geom::geography, s.geom::geography) AS distance
        FROM static.location l
        WHERE l.load_id = %(jdf)s AND l.kind = 'stop_place' AND l.domain = 'surface'
            AND l.geom && ST_Expand(s.geom, %(degrees)s)
            AND ST_DWithin(l.geom::geography, s.geom::geography, %(radius)s)
            AND EXISTS (
                SELECT 1 FROM static.location_mode m
                WHERE m.load_id = l.load_id AND m.location_id = l.location_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM overrides o
                WHERE o.decision = 'apart' AND o.station_id = s.location_id
                    AND o.stop_id = l.location_id
            )
        ORDER BY distance, l.location_id
        LIMIT %(candidates)s
    ) c
),
seen AS (
    SELECT station_id,
           (array_agg(stop_id ORDER BY rank) FILTER (WHERE named))[1] AS named1,
           (array_agg(distance ORDER BY rank) FILTER (WHERE named))[1] AS named1_m,
           (array_agg(distance ORDER BY rank) FILTER (WHERE named))[2] AS named2_m,
           (array_agg(stop_id ORDER BY rank))[1] AS near1,
           (array_agg(distance ORDER BY rank))[1] AS near1_m,
           (array_agg(distance ORDER BY rank))[2] AS near2_m,
           jsonb_agg(jsonb_build_object(
               'stop_id', stop_id, 'name', name, 'distance_m', round(distance::numeric),
               'named', named) ORDER BY rank) AS candidates
    FROM ranked
    GROUP BY station_id
),
ruled AS (
    SELECT s.location_id AS station_id, coalesce(v.candidates, '[]'::jsonb) AS candidates,
           CASE
               WHEN v.named1 IS NOT NULL
                   AND (v.named2_m IS NULL OR v.named2_m - v.named1_m >= %(margin)s) THEN 'named'
               WHEN v.named1 IS NOT NULL THEN 'two_named'
               WHEN v.near1 IS NOT NULL
                   AND (v.near2_m IS NULL OR v.near2_m - v.near1_m >= %(margin)s) THEN 'nearest'
               WHEN v.near1 IS NOT NULL THEN 'tie'
               ELSE 'nothing_near'
           END AS reason,
           v.named1, v.near1
    FROM stations s
    LEFT JOIN seen v ON v.station_id = s.location_id
),
forced AS (
    SELECT DISTINCT ON (o.station_id) o.station_id, o.stop_id
    FROM overrides o
    JOIN static.location l
        ON l.load_id = %(jdf)s AND l.location_id = o.stop_id AND l.kind = 'stop_place'
        AND l.domain = 'surface'
    WHERE o.decision = 'pair'
    ORDER BY o.station_id, o.stop_id
),
decided AS (
    SELECT r.station_id, r.candidates,
           CASE r.reason WHEN 'named' THEN r.named1 WHEN 'nearest' THEN r.near1 END
               AS rule_stop_id,
           f.stop_id AS forced_stop_id,
           EXISTS (
               SELECT 1 FROM overrides o
               WHERE o.decision = 'apart' AND o.station_id = r.station_id AND o.stop_id = ''
           ) AS kept_apart,
           r.reason
    FROM ruled r
    LEFT JOIN forced f ON f.station_id = r.station_id
)
INSERT INTO control.station_pairing
    (jdf_load_id, czptt_load_id, station_id, stop_id, outcome, reason, rule_stop_id, candidates)
SELECT %(jdf)s, %(czptt)s, d.station_id,
       CASE WHEN d.forced_stop_id IS NOT NULL THEN d.forced_stop_id
            WHEN d.kept_apart THEN NULL
            ELSE d.rule_stop_id END,
       CASE WHEN d.forced_stop_id IS NOT NULL THEN 'paired'
            WHEN d.kept_apart THEN 'apart'
            WHEN d.reason IN ('named', 'nearest') THEN 'paired'
            WHEN d.reason IN ('two_named', 'tie') THEN 'ambiguous'
            ELSE 'none' END,
       CASE WHEN d.forced_stop_id IS NOT NULL THEN 'override'
            WHEN d.kept_apart THEN 'override_apart'
            ELSE d.reason END,
       d.rule_stop_id, d.candidates
FROM decided d
