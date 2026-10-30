-- The typed features and notes of many trips (trip_features.sql without the route's): of
-- each trip, its calls and its service, by trip.
SELECT a.trip_id, a.scope, a.kind, a.call_sequence, a.call_sequence_to, a.source_code,
       n.kind AS note_kind, n.label AS note_label, n.text AS note_text,
       n.service_note_type AS note_code, a.call_sequence AS sort_sequence, a.assignment_id
FROM active.assignment a
LEFT JOIN active.service_note n ON n.note_id = a.note_id
WHERE a.scope IN ('trip', 'call', 'call_range') AND a.trip_id = ANY(%(trip_ids)s::text[])
UNION ALL
SELECT t.trip_id, a.scope, a.kind, a.call_sequence, a.call_sequence_to, a.source_code,
       n.kind, n.label, n.text, n.service_note_type, a.call_sequence, a.assignment_id
FROM active.trip t
JOIN active.assignment a ON a.scope = 'service' AND a.service_id = t.service_id
LEFT JOIN active.service_note n ON n.note_id = a.note_id
WHERE t.trip_id = ANY(%(trip_ids)s::text[])
ORDER BY trip_id, sort_sequence NULLS FIRST, scope, kind, assignment_id
