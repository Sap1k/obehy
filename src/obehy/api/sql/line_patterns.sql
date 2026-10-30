-- A line's trips running on the date, by first departure, for its overview: each with its
-- passenger calls' stops and scheduled times (departure, at the last stop arrival) in order.
WITH trips AS (
    SELECT t.trip_id, t.short_name, t.headsign, t.direction, t.shape_id,
           t.wheelchair_accessible, t.bikes_allowed
    FROM active.trip t
    JOIN active.service_date d
        ON d.service_id = t.service_id AND d.service_date = %(service_date)s
    WHERE t.route_id = %(route_id)s
)
SELECT t.trip_id, t.short_name AS trip_short_name, t.headsign, t.direction, t.shape_id,
       t.wheelchair_accessible, t.bikes_allowed,
       array_agg(c.location_id ORDER BY c.sequence) AS stops,
       array_agg(control.obehy_instant_fast(
           %(service_date)s, coalesce(c.scheduled_departure, c.scheduled_arrival))
           ORDER BY c.sequence) AS times
FROM trips t
JOIN active.trip_call c ON c.trip_id = t.trip_id AND c.passenger_service
GROUP BY t.trip_id, t.short_name, t.headsign, t.direction, t.shape_id, t.wheelchair_accessible,
         t.bikes_allowed
ORDER BY min(coalesce(c.scheduled_departure, c.scheduled_arrival)) NULLS LAST, t.trip_id
