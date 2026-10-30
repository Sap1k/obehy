-- A line's trips running between two service dates (the requested date, or from today on),
-- each with its calls in order: the passenger calls and the calls it runs through without
-- stopping, their scheduled times in seconds of the service day (and as instants on
-- %(dated)s's date), boarding codes and post.
WITH trips AS (
    SELECT t.trip_id, t.short_name AS trip_short_name, t.headsign, t.direction, t.service_id,
           t.wheelchair_accessible, t.bikes_allowed
    FROM active.trip t
    WHERE t.route_id = %(route_id)s
        AND EXISTS (
            SELECT 1 FROM active.service_date d
            WHERE d.service_id = t.service_id
                AND d.service_date BETWEEN %(since)s AND %(until)s
        )
)
SELECT t.trip_id, t.trip_short_name, t.headsign, t.direction, t.service_id,
       t.wheelchair_accessible, t.bikes_allowed,
       jsonb_agg(
           jsonb_build_object(
               'sequence', c.sequence, 'location_id', c.location_id, 'name', l.name,
               'passenger', c.passenger_service,
               'arrival_s', c.scheduled_arrival, 'departure_s', c.scheduled_departure,
               'arrival', CASE WHEN %(dated)s
                   THEN control.obehy_instant_fast(%(since)s, c.scheduled_arrival) END,
               'departure', CASE WHEN %(dated)s
                   THEN control.obehy_instant_fast(%(since)s, c.scheduled_departure) END,
               'pickup_type', c.pickup_type, 'dropoff_type', c.dropoff_type,
               'platform', bp.public_code
           )
           ORDER BY c.sequence
       ) AS calls
FROM trips t
JOIN active.trip_call c ON c.trip_id = t.trip_id
JOIN active.location l ON l.location_id = c.location_id
LEFT JOIN active.location bp ON bp.location_id = c.boarding_point_id
GROUP BY t.trip_id, t.trip_short_name, t.headsign, t.direction, t.service_id,
         t.wheelchair_accessible, t.bikes_allowed
ORDER BY t.trip_id
