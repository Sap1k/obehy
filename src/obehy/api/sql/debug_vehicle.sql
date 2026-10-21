-- Why a vehicle is what it is: its source reports and the key that bound each, the registry
-- keys naming it, the pairings of its reports, what each of them states, the curated values
-- and the profile the worker resolved.
WITH reports AS (
    SELECT v.*
    FROM rt.vehicle_state_current v
    WHERE v.vehicle_id = %(vehicle_id)s
),
named AS (
    SELECT source, source_vehicle_id FROM reports
    UNION
    SELECT source, source_vehicle_id
    FROM ref.vehicle_key
    WHERE vehicle_id = %(vehicle_id)s AND source IS NOT NULL
)
SELECT 'report' AS row_kind, to_jsonb(r) AS body
FROM reports r
UNION ALL
SELECT 'key', to_jsonb(k) || jsonb_build_object('priority', d.priority)
FROM ref.vehicle_key k
JOIN ref.dataset d USING (dataset)
WHERE k.vehicle_id = %(vehicle_id)s
UNION ALL
SELECT 'pairing', to_jsonb(p)
FROM rt.vehicle_pairing p
WHERE EXISTS (
    SELECT 1 FROM named n
    WHERE (n.source, n.source_vehicle_id) IN
        ((p.source_a, p.source_vehicle_a), (p.source_b, p.source_vehicle_b))
)
UNION ALL
SELECT 'observed', to_jsonb(o)
FROM rt.vehicle_attribute_observed o
JOIN named n USING (source, source_vehicle_id)
UNION ALL
SELECT 'curated', to_jsonb(a)
FROM ref.vehicle_attribute a
WHERE a.vehicle_id = %(vehicle_id)s
UNION ALL
SELECT 'profile', to_jsonb(p)
FROM rt.vehicle_profile p
WHERE p.vehicle_id = %(vehicle_id)s
