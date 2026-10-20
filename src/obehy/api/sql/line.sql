-- A line with its operator.
SELECT r.route_id, r.short_name, r.long_name, r.mode, r.color, r.text_color, r.description,
       a.name AS agency
FROM active.route r
LEFT JOIN active.agency a ON a.agency_id = r.agency_id
WHERE r.route_id = %(route_id)s
