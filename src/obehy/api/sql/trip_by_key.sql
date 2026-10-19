-- The trips a source key names that run on the date (a rail run's parts share its run key).
SELECT DISTINCT t.trip_id, t.run_key, t.run_part
FROM active.source_key k
JOIN active.trip t ON t.trip_id = k.public_id
JOIN active.service_date d ON d.service_id = t.service_id AND d.service_date = %(service_date)s
WHERE k.namespace = %(namespace)s AND k.identifier = %(key)s AND k.entity_kind = 'trip'
    AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
ORDER BY t.run_key NULLS FIRST, t.run_part NULLS FIRST, t.trip_id
