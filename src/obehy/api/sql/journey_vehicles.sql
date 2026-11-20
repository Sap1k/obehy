-- The vehicles that worked a journey on each service date between two dates: each source's
-- report, the vehicle the worker resolved it as and its fleet number, and whether the source's
-- ID is a vehicle on the mode the journey ran as (its latest schedule revision).
SELECT a.service_date, a.source, a.source_vehicle_id, a.vehicle_id, vp.fleet_number,
       vp.public_label, rt.is_physical(a.source, s.mode) AS physical
FROM history.vehicle_assignment a
LEFT JOIN history.journey_latest s USING (feed, key_namespace, key, service_date)
LEFT JOIN rt.vehicle_profile vp ON vp.vehicle_id = a.vehicle_id AND vp.registered
WHERE a.key_namespace = %(namespace)s AND a.key = %(key)s
    AND a.service_date BETWEEN %(since)s AND %(until)s
ORDER BY a.service_date, a.vehicle_id, a.source, a.source_vehicle_id
