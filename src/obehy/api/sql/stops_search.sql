-- Stop places whose name contains the words in order, regardless of case and diacritics; names
-- starting with the first word first, then the closest. The words come with LIKE wildcards
-- escaped. A station and the stop in front of it (active.stop_group) are one result: the
-- best-placed member, with its members (stations first) and what calls at each.
WITH q AS (
    SELECT '%%' || array_to_string(
               ARRAY(SELECT control.obehy_fold(w) FROM unnest(%(words)s::text[]) AS w), '%%'
           ) || '%%' AS pattern,
           control.obehy_fold(%(first)s) || '%%' AS prefix,
           control.obehy_fold(%(text)s) AS folded
),
found AS (
    SELECT l.location_id, g.group_id,
           row_number() OVER (
               ORDER BY control.obehy_fold(l.name) LIKE q.prefix DESC,
                   similarity(control.obehy_fold(l.name), q.folded) DESC, l.name, l.location_id
           ) AS rank
    FROM active.location l
    CROSS JOIN q
    LEFT JOIN active.stop_group g ON g.location_id = l.location_id
    WHERE l.kind = 'stop_place' AND control.obehy_fold(l.name) LIKE q.pattern
),
best AS (
    SELECT DISTINCT ON (coalesce(group_id, location_id)) location_id, group_id, rank
    FROM found
    ORDER BY coalesce(group_id, location_id), rank
),
shown AS (
    SELECT location_id, group_id, rank FROM best ORDER BY rank LIMIT %(limit)s
)
SELECT l.location_id, l.name, l.kind, l.domain, l.municipality_name AS municipality,
       l.district_name AS district, l.latitude, l.longitude, l.coordinate_precision,
       ARRAY(SELECT m.mode FROM active.location_mode m WHERE m.location_id = l.location_id)
           AS modes,
       (SELECT jsonb_agg(jsonb_build_object(
                   'location_id', ml.location_id, 'name', ml.name, 'domain', ml.domain,
                   'modes', ARRAY(SELECT m.mode FROM active.location_mode m
                                  WHERE m.location_id = ml.location_id))
                   ORDER BY g.station DESC, ml.name, g.location_id)
        FROM active.stop_group g
        JOIN active.location ml ON ml.location_id = g.location_id
        WHERE g.group_id = s.group_id) AS "group"
FROM shown s
JOIN active.location l ON l.location_id = s.location_id
ORDER BY s.rank
