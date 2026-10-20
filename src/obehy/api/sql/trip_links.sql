-- The journeys linked to a trip's journey on the date (a train continuing under another number,
-- splitting, joining), each with the trip of the active release it names, if any.
WITH links AS (
    SELECT j.kind, j.target_namespace AS namespace, j.target_key AS key
    FROM history.journey_link j
    WHERE j.service_date = %(service_date)s AND j.source_namespace = %(namespace)s
        AND j.source_key = %(key)s
    UNION ALL
    SELECT CASE j.kind WHEN 'continues_as' THEN 'continued_from' ELSE j.kind END,
           j.source_namespace, j.source_key
    FROM history.journey_link j
    WHERE j.service_date = %(service_date)s AND j.target_namespace = %(namespace)s
        AND j.target_key = %(key)s
)
SELECT DISTINCT ON (l.kind, l.namespace, l.key) l.kind, l.namespace, l.key, t.trip_id
FROM links l
LEFT JOIN active.source_key k
    ON k.namespace = l.namespace AND k.identifier = l.key AND k.entity_kind = 'trip'
    AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
LEFT JOIN active.trip t ON t.trip_id = k.public_id
    AND EXISTS (
        SELECT 1 FROM active.service_date d
        WHERE d.service_id = t.service_id AND d.service_date = %(service_date)s
    )
ORDER BY l.kind, l.namespace, l.key, t.trip_id IS NULL, t.run_part NULLS LAST, t.trip_id
