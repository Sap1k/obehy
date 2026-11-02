-- The locations an overlaid feed's stop IDs name on the date (its source keys).
SELECT DISTINCT k.identifier, k.public_id
FROM active.source_key k
WHERE k.namespace = %(namespace)s AND k.identifier = ANY(%(identifiers)s::text[])
    AND k.entity_kind = 'location' AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
ORDER BY k.identifier, k.public_id
