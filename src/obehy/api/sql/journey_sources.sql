-- The source vehicles that worked any of a trip's journey keys on the date.
SELECT DISTINCT source, source_vehicle_id
FROM history.vehicle_assignment
WHERE service_date = %(service_date)s
    AND (key_namespace, key) IN (SELECT * FROM unnest(%(namespaces)s::text[], %(keys)s::text[]))
ORDER BY source, source_vehicle_id
