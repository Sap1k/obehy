-- A trip of the active release, with its line and whether it runs on the service date.
SELECT t.trip_id, t.route_id, t.service_id, t.headsign, t.short_name AS trip_short_name,
       t.run_key, t.wheelchair_accessible, t.bikes_allowed, t.shape_id, r.short_name,
       r.long_name, r.mode, r.color, r.text_color, a.name AS agency,
       EXISTS (
           SELECT 1 FROM active.service_date d
           WHERE d.service_id = t.service_id AND d.service_date = %(service_date)s
       ) AS runs
FROM active.trip t
JOIN active.route r ON r.route_id = t.route_id
LEFT JOIN active.agency a ON a.agency_id = r.agency_id
WHERE t.trip_id = %(trip_id)s
