-- The lines with a passenger call at a stop place.
SELECT DISTINCT r.route_id, r.short_name, r.long_name, r.mode, r.color, r.text_color,
       r.sort_order
FROM active.trip_call c
JOIN active.trip t ON t.trip_id = c.trip_id
JOIN active.route r ON r.route_id = t.route_id
WHERE c.location_id = %(location_id)s AND c.passenger_service
ORDER BY r.sort_order NULLS LAST, r.short_name NULLS LAST, r.route_id
