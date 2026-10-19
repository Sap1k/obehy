-- The trip's journey key on the date, in the namespaces' order of preference.
SELECT k.namespace, k.identifier AS key
FROM active.source_key k
WHERE k.public_id = %(trip_id)s AND k.entity_kind = 'trip'
    AND k.namespace = ANY(%(namespaces)s::text[])
    AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
ORDER BY array_position(%(namespaces)s::text[], k.namespace), k.identifier
LIMIT 1
