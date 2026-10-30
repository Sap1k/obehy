-- A source's journeys on a date in a scope: of the given agencies (and modes, when given) or of
-- one route, by the active release's trip each key names that date. The vehicles that worked
-- them are the scope's rows.
SELECT DISTINCT a.source_vehicle_id, a.key_namespace, a.key
FROM history.vehicle_assignment a
JOIN active.source_key k
    ON k.entity_kind = 'trip' AND (k.namespace, k.identifier) = (a.key_namespace, a.key)
    AND a.service_date BETWEEN k.valid_from AND k.valid_to
JOIN active.trip t ON t.trip_id = k.public_id
JOIN active.route r ON r.route_id = t.route_id
WHERE a.source = %(source)s AND a.service_date = %(service_date)s
    AND CASE
        WHEN %(route_id)s::text IS NOT NULL THEN r.route_id = %(route_id)s
        ELSE r.agency_id = ANY(%(agency_ids)s::text[])
            AND (%(modes)s::text[] IS NULL OR r.mode = ANY(%(modes)s::text[]))
    END
ORDER BY a.source_vehicle_id, a.key_namespace, a.key
