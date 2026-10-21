-- The typed features of a stop place: facilities and interchanges.
SELECT a.scope, a.kind, a.call_sequence, a.call_sequence_to, a.source_code,
       NULL::text AS note_kind, NULL::text AS note_label, NULL::text AS note_text,
       NULL::text AS note_code
FROM active.assignment a
WHERE a.scope = 'location' AND a.location_id = %(location_id)s
ORDER BY a.kind, a.assignment_id
