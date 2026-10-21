-- The trip split at every unit span boundary, each piece with the units running it, in train
-- order per source (BASE_PLAN.md section 28). Pieces no unit runs are left out.
WITH units AS (
    SELECT *
    FROM rt.composition_spans(
        %(trip_id)s, %(feed)s, %(key_namespace)s, %(key)s, %(service_date)s
    )
),
bounds AS (
    SELECT DISTINCT b
    FROM units, LATERAL (VALUES (units.from_sequence), (units.to_sequence)) v (b)
),
pieces AS (
    SELECT b AS from_sequence, lead(b) OVER (ORDER BY b) AS to_sequence FROM bounds
)
SELECT p.from_sequence, p.to_sequence,
       jsonb_agg(
           jsonb_build_array(u.composition_id, u.unit_sequence)
           ORDER BY u.source, u.composition_id, u.unit_sequence
       ) AS units
FROM pieces p
JOIN units u ON u.from_sequence <= p.from_sequence AND u.to_sequence >= p.to_sequence
GROUP BY p.from_sequence, p.to_sequence
ORDER BY p.from_sequence
