-- A line's own features and notes: the assignments of its route as a whole.
SELECT a.scope, a.kind, a.call_sequence, a.call_sequence_to, a.source_code,
       n.kind AS note_kind, n.label AS note_label, n.text AS note_text,
       n.service_note_type AS note_code
FROM active.assignment a
LEFT JOIN active.service_note n ON n.note_id = a.note_id
WHERE a.scope = 'route' AND a.route_id = %(route_id)s
ORDER BY a.kind, a.assignment_id
