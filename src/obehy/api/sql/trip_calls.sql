-- A trip's passenger calls with their realtime state: the trip's slice of the current
-- journey's calls (a rail run's part, rt.trip_current), matched by sequence. Railway points are
-- not public stops. A call is placed at its post when the post's position is known exactly (not
-- JrUtil's estimate), else at its stop place.
WITH live AS (
    SELECT e.value AS call
    FROM rt.trip_current(%(trip_id)s, %(service_date)s, %(release_id)s) x,
    jsonb_array_elements(x.calls) AS e (value)
)
SELECT c.sequence, c.location_id, l.name,
       CASE WHEN bp.coordinate_precision = 'exact' THEN bp.latitude ELSE l.latitude END AS latitude,
       CASE WHEN bp.coordinate_precision = 'exact' THEN bp.longitude ELSE l.longitude END
           AS longitude,
       c.pickup_type,
       c.dropoff_type, bp.public_code AS platform,
       control.obehy_instant_fast(%(service_date)s, c.scheduled_arrival) AS scheduled_arrival,
       control.obehy_instant_fast(%(service_date)s, c.scheduled_departure) AS scheduled_departure,
       (v.call ->> 'estimated_arrival')::timestamptz AS estimated_arrival,
       (v.call ->> 'estimated_departure')::timestamptz AS estimated_departure,
       coalesce(v.call ->> 'status', 'scheduled') AS status,
       v.call ->> 'source_class' AS source_class,
       coalesce((v.call ->> 'passed')::boolean, false) AS live_passed
FROM active.trip_call c
JOIN active.location l ON l.location_id = c.location_id
LEFT JOIN active.location bp ON bp.location_id = c.boarding_point_id
LEFT JOIN live v ON (v.call ->> 'sequence')::integer = c.sequence
WHERE c.trip_id = %(trip_id)s AND c.passenger_service
ORDER BY c.sequence
