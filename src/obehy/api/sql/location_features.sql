-- The typed features of many stop places (stop_features.sql for a timetable's rows), by
-- stop.
SELECT a.location_id, a.scope, a.kind, a.call_sequence, a.call_sequence_to, a.source_code,
       NULL::text AS note_kind, NULL::text AS note_label, NULL::text AS note_text,
       NULL::text AS note_code
FROM active.assignment a
WHERE a.scope = 'location' AND a.location_id = ANY(%(location_ids)s::text[])
ORDER BY a.location_id, a.kind, a.assignment_id
