-- The lines a source's vehicles worked on a date, as `circulation_scopes.sql` resolves their
-- journeys, in distinct vehicles: the lines the day's oběhy can be scoped by.
WITH worked AS (
    SELECT DISTINCT a.source_vehicle_id, t.route_id
    FROM history.vehicle_assignment a
    JOIN active.source_key k
        ON k.entity_kind = 'trip' AND (k.namespace, k.identifier) = (a.key_namespace, a.key)
        AND a.service_date BETWEEN k.valid_from AND k.valid_to
    JOIN active.trip t ON t.trip_id = k.public_id
    WHERE a.source = %(source)s AND a.service_date = %(service_date)s
)
SELECT r.route_id, r.short_name, r.long_name, r.mode, r.color, r.text_color, ag.name AS agency,
       count(DISTINCT w.source_vehicle_id) AS vehicles
FROM worked w
JOIN active.route r ON r.route_id = w.route_id
LEFT JOIN active.agency ag ON ag.agency_id = r.agency_id
GROUP BY r.route_id, r.short_name, r.long_name, r.mode, r.color, r.text_color, ag.name,
    r.sort_order
ORDER BY r.sort_order NULLS LAST, length(coalesce(r.short_name, '')), r.short_name, r.route_id
