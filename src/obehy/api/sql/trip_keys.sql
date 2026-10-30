-- Every journey key of a trip valid on the date, whatever its namespace: the keys its vehicles
-- were assigned under.
SELECT k.namespace, k.identifier AS key
FROM active.source_key k
WHERE k.public_id = %(trip_id)s AND k.entity_kind = 'trip'
    AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
ORDER BY k.namespace, k.identifier
