-- Stop places whose name contains the words in order, regardless of case and diacritics; names
-- starting with the first word first, then the closest. The words come with LIKE wildcards
-- escaped.
WITH q AS (
    SELECT '%%' || array_to_string(
               ARRAY(SELECT control.obehy_fold(w) FROM unnest(%(words)s::text[]) AS w), '%%'
           ) || '%%' AS pattern,
           control.obehy_fold(%(first)s) || '%%' AS prefix,
           control.obehy_fold(%(text)s) AS folded
)
SELECT l.location_id, l.name, l.kind, l.domain, l.municipality_name AS municipality,
       l.district_name AS district, l.latitude, l.longitude, l.coordinate_precision
FROM active.location l, q
WHERE l.kind = 'stop_place' AND control.obehy_fold(l.name) LIKE q.pattern
ORDER BY control.obehy_fold(l.name) LIKE q.prefix DESC,
    similarity(control.obehy_fold(l.name), q.folded) DESC, l.name, l.location_id
LIMIT %(limit)s
