-- The raw assignment rows of a trip, its route and its service: the source symbols behind the
-- readable features.
SELECT a.assignment_id, a.scope, a.kind, a.source_code, a.call_sequence, a.call_sequence_to,
       a.note_id, a.source_object_id
FROM active.assignment a
WHERE (a.scope IN ('trip', 'call', 'call_range') AND a.trip_id = %(trip_id)s)
    OR (a.scope = 'route' AND a.route_id = %(route_id)s)
    OR (a.scope = 'service' AND a.service_id = %(service_id)s)
ORDER BY a.assignment_id
