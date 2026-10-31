-- The vehicles that worked a journey on each service date between two dates: each source's
-- report, the vehicle the registry knows it as (history.vehicle_of) and its fleet number, and
-- the mode the journey ran as (its latest schedule revision), for whether its ID is a vehicle.
SELECT a.service_date, a.source, a.source_vehicle_id,
       history.vehicle_of(a.source, a.source_vehicle_id, a.service_date) AS vehicle_id,
       vp.fleet_number, vp.public_label, s.mode
FROM history.vehicle_assignment a
LEFT JOIN history.journey j USING (feed, key_namespace, key, service_date)
LEFT JOIN history.journey_schedule s
    ON (s.feed, s.key_namespace, s.key, s.service_date, s.revision)
        = (j.feed, j.key_namespace, j.key, j.service_date, j.latest_revision)
LEFT JOIN rt.vehicle_profile vp
    ON vp.vehicle_id = history.vehicle_of(a.source, a.source_vehicle_id, a.service_date)
    AND vp.registered
WHERE a.key_namespace = %(namespace)s AND a.key = %(key)s
    AND a.service_date BETWEEN %(since)s AND %(until)s
ORDER BY a.service_date, vehicle_id, a.source, a.source_vehicle_id
