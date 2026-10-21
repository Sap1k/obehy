-- The journey's composition units from every source, as spans over the trip's calls.
SELECT *
FROM rt.composition_spans(%(trip_id)s, %(feed)s, %(key_namespace)s, %(key)s, %(service_date)s)
ORDER BY from_sequence, source, composition_id, unit_sequence
