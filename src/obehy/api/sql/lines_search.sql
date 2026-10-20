-- Lines whose short or long name contains the words in order, regardless of case and
-- diacritics; an exact short name first, then short names starting with the first word.
WITH q AS (
    SELECT '%%' || array_to_string(
               ARRAY(SELECT control.obehy_fold(w) FROM unnest(%(words)s::text[]) AS w), '%%'
           ) || '%%' AS pattern,
           control.obehy_fold(%(first)s) AS first,
           control.obehy_fold(%(text)s) AS folded
)
SELECT r.route_id, r.short_name, r.long_name, r.mode, r.color, r.text_color, a.name AS agency
FROM active.route r
CROSS JOIN q
LEFT JOIN active.agency a ON a.agency_id = r.agency_id
WHERE control.obehy_fold(coalesce(r.short_name, '') || ' ' || coalesce(r.long_name, ''))
    LIKE q.pattern
ORDER BY control.obehy_fold(coalesce(r.short_name, '')) = q.folded DESC,
    control.obehy_fold(coalesce(r.short_name, '')) LIKE q.first || '%%' DESC,
    r.sort_order NULLS LAST, length(coalesce(r.short_name, '')), r.short_name, r.route_id
LIMIT %(limit)s
