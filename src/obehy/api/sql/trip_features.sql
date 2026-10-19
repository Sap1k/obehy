-- Typed features and notes of a trip, its calls, its route and its service.
SELECT a.scope, a.kind, a.call_sequence, a.call_sequence_to, a.source_code,
       n.kind AS note_kind, n.label AS note_label, n.text AS note_text
FROM active.assignment a
LEFT JOIN active.service_note n ON n.note_id = a.note_id
WHERE (a.scope IN ('trip', 'call', 'call_range') AND a.trip_id = %(trip_id)s)
    OR (a.scope = 'route' AND a.route_id = %(route_id)s)
    OR (a.scope = 'service' AND a.service_id = %(service_id)s)
ORDER BY a.call_sequence NULLS FIRST, a.scope, a.kind, a.assignment_id
