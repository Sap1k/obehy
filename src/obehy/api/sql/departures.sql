-- A stop place's departures: passenger calls with pickup, not the trip's last passenger call,
-- of trips running on their service date. `spans` prefilters each service date's scheduled
-- seconds (with DST slack); the exact window is on instants. A departure scheduled from `since`
-- (a lookback before `start`) stays while its expected time is still at or after `start`.
-- Realtime is found by the feed trip ID through rt.trip_part_current, in the active release;
-- the part's slice of the journey's calls holds the call by its sequence.
WITH spans (service_date, lo, hi) AS (
    SELECT * FROM unnest(%(dates)s::date[], %(lo)s::integer[], %(hi)s::integer[])
),
-- The stop's calls first, by its index (materialized: estimated inside the join, the planner
-- scans the whole table instead), then the window.
at_stop AS MATERIALIZED (
    SELECT c.trip_id, c.sequence, c.boarding_point_id, c.stop_headsign, c.scheduled_departure
    FROM active.trip_call c
    WHERE c.location_id = %(location_id)s AND c.passenger_service AND c.pickup_type <> 1
),
board AS MATERIALIZED (
    SELECT a.trip_id, a.sequence, a.boarding_point_id, a.stop_headsign, s.service_date,
           control.obehy_instant_fast(s.service_date, a.scheduled_departure) AS scheduled
    FROM at_stop a
    JOIN spans s ON a.scheduled_departure BETWEEN s.lo AND s.hi
    WHERE EXISTS (
        SELECT 1 FROM active.trip_call n
        WHERE n.trip_id = a.trip_id AND n.sequence > a.sequence AND n.passenger_service
    )
),
-- The stop's own platforms and posts: the codes of its calls' boarding points.
posts AS (
    SELECT location_id, public_code
    FROM active.location
    WHERE parent_location_id = %(location_id)s
),
running AS (
    SELECT b.*, t.route_id, t.headsign, t.short_name
    FROM board b
    JOIN active.trip t ON t.trip_id = b.trip_id
    JOIN active.service_date d
        ON d.service_id = t.service_id AND d.service_date = b.service_date
    WHERE b.scheduled >= %(since)s AND b.scheduled < %(until)s
),
-- The realtime calls of the running trips: each trip's slice of its current journey's calls
-- (the freshest journey, should two hold the trip).
realtime AS (
    SELECT DISTINCT ON (p.trip_id, p.service_date, (e.value ->> 'sequence')::integer)
           p.trip_id, p.service_date, (e.value ->> 'sequence')::integer AS sequence,
           e.value AS call, s.stale
    FROM rt.trip_part_current p
    JOIN rt.trip_state_current s USING (feed, key_namespace, key, service_date, release_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.calls) WITH ORDINALITY AS e (value, n)
    WHERE p.release_id = %(release_id)s
        AND (p.trip_id, p.service_date) IN (SELECT trip_id, service_date FROM running)
        AND e.n - 1 BETWEEN p.first_call AND p.last_call
    ORDER BY p.trip_id, p.service_date, (e.value ->> 'sequence')::integer, s.updated_at DESC
),
live AS (
    SELECT r.*, x.call, x.stale, (x.call ->> 'estimated_departure')::timestamptz AS estimated
    FROM running r
    LEFT JOIN realtime x USING (trip_id, service_date, sequence)
)
SELECT l.trip_id, l.service_date, l.route_id, ro.short_name, ro.long_name, ro.mode, ro.color,
       ro.text_color, l.short_name AS trip_short_name,
       coalesce(l.stop_headsign, l.headsign) AS headsign, l.scheduled, l.estimated,
       extract(epoch FROM l.estimated - l.scheduled)::integer AS delay_s,
       coalesce(l.call ->> 'status', 'scheduled') AS status,
       l.call ->> 'source_class' AS source_class, l.stale, bp.public_code AS platform
FROM live l
JOIN active.route ro ON ro.route_id = l.route_id
LEFT JOIN posts bp ON bp.location_id = l.boarding_point_id
WHERE coalesce(l.estimated, l.scheduled) >= %(start)s
    AND coalesce(l.estimated, l.scheduled) < %(until)s
ORDER BY coalesce(l.estimated, l.scheduled), l.scheduled, l.trip_id
LIMIT %(limit)s
