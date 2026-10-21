-- Reports of two sources seen running one journey side by side, freshest first: candidate
-- registry keys. Each side with the vehicle it is now and how it was bound.
SELECT p.source_a, p.source_vehicle_a, a.vehicle_id AS vehicle_a, a.bound_by AS bound_by_a,
       p.source_b, p.source_vehicle_b, b.vehicle_id AS vehicle_b, b.bound_by AS bound_by_b,
       p.first_seen, p.last_seen, p.fixes, p.confirmed, p.journey
FROM rt.vehicle_pairing p
LEFT JOIN LATERAL (
    SELECT v.vehicle_id, v.bound_by
    FROM rt.vehicle_state_current v
    WHERE (v.source, v.source_vehicle_id) = (p.source_a, p.source_vehicle_a)
    ORDER BY v.last_seen DESC
    LIMIT 1
) a ON true
LEFT JOIN LATERAL (
    SELECT v.vehicle_id, v.bound_by
    FROM rt.vehicle_state_current v
    WHERE (v.source, v.source_vehicle_id) = (p.source_b, p.source_vehicle_b)
    ORDER BY v.last_seen DESC
    LIMIT 1
) b ON true
WHERE (%(confirmed)s::boolean IS NULL OR p.confirmed = %(confirmed)s)
ORDER BY p.last_seen DESC, p.source_a, p.source_vehicle_a, p.source_b, p.source_vehicle_b
LIMIT %(limit)s
