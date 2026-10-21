-- The first and last service date a line runs in the release.
SELECT min(d.service_date) AS first_date, max(d.service_date) AS last_date
FROM active.service_date d
WHERE d.service_id IN (SELECT t.service_id FROM active.trip t WHERE t.route_id = %(route_id)s)
