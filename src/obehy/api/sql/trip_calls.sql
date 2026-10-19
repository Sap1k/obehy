-- A trip's passenger calls with their realtime state: the trip's slice of the current journey's
-- calls (a rail run's part), matched by sequence. Railway points are not public stops.
WITH state AS (
    SELECT s.calls, p.first_call, p.last_call
    FROM rt.trip_part_current p
    JOIN rt.trip_state_current s USING (feed, key_namespace, key, service_date, release_id)
    WHERE p.trip_id = %(trip_id)s AND p.service_date = %(service_date)s
        AND p.release_id = %(release_id)s
    ORDER BY s.updated_at DESC
    LIMIT 1
),
live AS (
    SELECT e.value AS call
    FROM state, jsonb_array_elements(state.calls) WITH ORDINALITY AS e (value, n)
    WHERE e.n - 1 BETWEEN state.first_call AND state.last_call
)
SELECT c.sequence, c.location_id, l.name, l.latitude, l.longitude, c.pickup_type,
       c.dropoff_type, bp.public_code AS platform,
       control.obehy_instant_fast(%(service_date)s, c.scheduled_arrival) AS scheduled_arrival,
       control.obehy_instant_fast(%(service_date)s, c.scheduled_departure) AS scheduled_departure,
       (v.call ->> 'estimated_arrival')::timestamptz AS estimated_arrival,
       (v.call ->> 'estimated_departure')::timestamptz AS estimated_departure,
       coalesce(v.call ->> 'status', 'scheduled') AS status,
       v.call ->> 'source_class' AS source_class
FROM active.trip_call c
JOIN active.location l ON l.location_id = c.location_id
LEFT JOIN active.location bp ON bp.location_id = c.boarding_point_id
LEFT JOIN live v ON (v.call ->> 'sequence')::integer = c.sequence
WHERE c.trip_id = %(trip_id)s AND c.passenger_service
ORDER BY c.sequence
