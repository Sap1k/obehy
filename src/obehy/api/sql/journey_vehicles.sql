-- The vehicles that worked a journey on each service date between two dates, as the registry
-- knows them (history.vehicle_of).
SELECT service_date,
       array_agg(DISTINCT history.vehicle_of(source, source_vehicle_id, service_date)
           ORDER BY history.vehicle_of(source, source_vehicle_id, service_date)) AS vehicles
FROM history.vehicle_assignment
WHERE key_namespace = %(namespace)s AND key = %(key)s
    AND service_date BETWEEN %(since)s AND %(until)s
GROUP BY service_date
