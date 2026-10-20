-- A line's trips running on the date, by first departure, with their realtime state.
WITH trips AS (
    SELECT t.trip_id, t.short_name, t.headsign
    FROM active.trip t
    JOIN active.service_date d
        ON d.service_id = t.service_id AND d.service_date = %(service_date)s
    WHERE t.route_id = %(route_id)s
),
ends AS (
    SELECT t.trip_id, t.short_name, t.headsign,
           min(c.scheduled_departure) FILTER (WHERE c.passenger_service) AS first_s,
           max(c.scheduled_arrival) FILTER (WHERE c.passenger_service) AS last_s
    FROM trips t
    JOIN active.trip_call c ON c.trip_id = t.trip_id
    GROUP BY t.trip_id, t.short_name, t.headsign
)
SELECT e.trip_id, e.short_name AS trip_short_name, e.headsign,
       control.obehy_instant_fast(%(service_date)s, e.first_s) AS first_departure,
       control.obehy_instant_fast(%(service_date)s, e.last_s) AS last_arrival,
       s.lifecycle, s.delay_s
FROM ends e
LEFT JOIN LATERAL (
    SELECT s.lifecycle, s.delay_s
    FROM rt.trip_part_current p
    JOIN rt.trip_state_current s USING (feed, key_namespace, key, service_date, release_id)
    WHERE p.trip_id = e.trip_id AND p.service_date = %(service_date)s
        AND p.release_id = %(release_id)s
    ORDER BY s.updated_at DESC
    LIMIT 1
) s ON true
ORDER BY e.first_s NULLS LAST, e.trip_id
