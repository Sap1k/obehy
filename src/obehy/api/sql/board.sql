-- A board of one or more stop places: departures (passenger calls with pickup, not a trip's
-- last passenger call) or, with `arrivals`, arrivals (calls with drop-off, not a trip's first
-- passenger call), of trips running on their service date. `spans` prefilters each service
-- date's scheduled seconds (with DST slack); the exact window is on instants. A row scheduled
-- from `since` (a lookback before `start`) stays while its expected time is still at or after
-- `start`. A cursor continues after the row it names. Realtime is found by the feed trip ID
-- through rt.trip_part_current, in the active release; the part's slice of the journey's
-- calls holds the call by its sequence, with the sources it came from. The vehicle is the
-- freshest one on the trip, with its profile's traits. `wheelchair_kinds` are what the
-- timetable states for the whole trip (its, its route's and its service's assignments),
-- evaluated for the page's rows only.
WITH spans (service_date, lo, hi) AS (
    SELECT * FROM unnest(%(dates)s::date[], %(lo)s::integer[], %(hi)s::integer[])
),
-- The stops' calls in each span first, by their index on the stop and the scheduled time (one
-- branch runs: the other's condition is false). Materialized: estimated inside the join, the
-- planner scans the whole table instead.
at_stop AS MATERIALIZED (
    SELECT c.trip_id, c.sequence, c.location_id, c.boarding_point_id, c.stop_headsign,
           s.service_date, c.scheduled_departure AS seconds
    FROM spans s
    JOIN active.trip_call c
        ON c.location_id = ANY(%(locations)s::text[])
        AND c.scheduled_departure BETWEEN s.lo AND s.hi
    WHERE NOT %(arrivals)s AND c.passenger_service AND c.pickup_type <> 1
    UNION ALL
    SELECT c.trip_id, c.sequence, c.location_id, c.boarding_point_id, c.stop_headsign,
           s.service_date, c.scheduled_arrival AS seconds
    FROM spans s
    JOIN active.trip_call c
        ON c.location_id = ANY(%(locations)s::text[])
        AND c.scheduled_arrival BETWEEN s.lo AND s.hi
    WHERE %(arrivals)s AND c.passenger_service AND c.dropoff_type <> 1
),
board AS MATERIALIZED (
    SELECT a.trip_id, a.sequence, a.location_id, a.boarding_point_id, a.stop_headsign,
           a.service_date, control.obehy_instant_fast(a.service_date, a.seconds) AS scheduled
    FROM at_stop a
),
-- The stops' own platforms and posts: the codes of their calls' boarding points.
posts AS (
    SELECT location_id, public_code
    FROM active.location
    WHERE parent_location_id = ANY(%(locations)s::text[])
),
running AS (
    SELECT b.*, t.route_id, t.service_id, t.headsign, t.short_name,
           t.wheelchair_accessible AS trip_wheelchair
    FROM board b
    JOIN active.trip t ON t.trip_id = b.trip_id
    JOIN active.service_date d
        ON d.service_id = t.service_id AND d.service_date = b.service_date
    WHERE b.scheduled >= %(since)s AND b.scheduled < %(until)s
        -- Not the trip's last (first) passenger call, checked once the trip runs on the date.
        AND EXISTS (
            SELECT 1 FROM active.trip_call n
            WHERE n.trip_id = b.trip_id AND n.passenger_service
                AND CASE WHEN %(arrivals)s THEN n.sequence < b.sequence
                         ELSE n.sequence > b.sequence END
        )
),
-- The realtime calls of the running trips: each trip's slice of its current journey's calls
-- (the freshest journey, should two hold the trip).
realtime AS (
    SELECT DISTINCT ON (p.trip_id, p.service_date, (e.value ->> 'sequence')::integer)
           p.trip_id, p.service_date, (e.value ->> 'sequence')::integer AS sequence,
           e.value AS call, s.stale,
           ARRAY(SELECT x ->> 'source' FROM jsonb_array_elements(s.sources) AS x) AS sources
    FROM rt.trip_part_current p
    JOIN rt.trip_state_current s USING (feed, key_namespace, key, service_date, release_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.calls) WITH ORDINALITY AS e (value, n)
    WHERE p.release_id = %(release_id)s
        AND (p.trip_id, p.service_date) IN (SELECT trip_id, service_date FROM running)
        AND e.n - 1 BETWEEN p.first_call AND p.last_call
    ORDER BY p.trip_id, p.service_date, (e.value ->> 'sequence')::integer, s.updated_at DESC
),
vehicles AS (
    SELECT DISTINCT ON (r.trip_id, r.service_date) r.*
    FROM (
        SELECT v.trip_id, v.service_date, v.vehicle_id, v.status, v.last_seen, v.position_at,
               v.latitude,
               v.longitude, v.bearing, v.source, v.source_vehicle_id, v.represents,
               array_agg(v.source) OVER (PARTITION BY v.trip_id, v.service_date, v.vehicle_id)
                   AS vehicle_sources
        FROM rt.vehicle_state_current v
        WHERE v.release_id = %(release_id)s AND v.last_seen >= %(seen_since)s
            AND (v.trip_id, v.service_date) IN (SELECT trip_id, service_date FROM running)
    ) r
    ORDER BY r.trip_id, r.service_date, r.represents DESC, r.last_seen DESC, r.source,
        r.source_vehicle_id
),
live AS (
    SELECT r.*, x.call, x.stale, x.sources,
           (x.call ->> CASE WHEN %(arrivals)s THEN 'estimated_arrival'
                            ELSE 'estimated_departure' END)::timestamptz AS estimated
    FROM running r
    LEFT JOIN realtime x USING (trip_id, service_date, sequence)
),
rows AS (
    SELECT l.*, coalesce(l.estimated, l.scheduled) AS expected
    FROM live l
)
SELECT l.trip_id, l.service_date, l.location_id, l.route_id, ro.short_name, ro.long_name,
       ro.mode, ro.color, ro.text_color, l.short_name AS trip_short_name,
       coalesce(l.stop_headsign, l.headsign) AS headsign, l.scheduled, l.estimated, l.expected,
       extract(epoch FROM l.estimated - l.scheduled)::integer AS delay_s,
       coalesce(l.call ->> 'status', 'scheduled') AS status,
       l.call ->> 'source_class' AS source_class, l.stale, l.sources, bp.public_code AS platform,
       v.vehicle_id, v.status AS vehicle_state, v.last_seen AS vehicle_seen,
       v.position_at AS vehicle_position_at,
       v.latitude AS vehicle_latitude, v.longitude AS vehicle_longitude,
       v.bearing AS vehicle_bearing, v.vehicle_sources, vp.low_floor, vp.air_conditioning,
       vp.wheelchair_accessible, vp.manufacturer, vp.model, vp.operator, vp.licence_plate, vp.fleet_number,
       l.trip_wheelchair,
       ARRAY(
           SELECT DISTINCT a.kind::text FROM active.assignment a
           WHERE a.kind IN ('wheelchair_accessible_vehicle', 'partly_wheelchair_accessible_vehicle',
                            'wheelchair_booking_required', 'wheelchair_booking_recommended')
               AND ((a.scope = 'trip' AND a.trip_id = l.trip_id)
                   OR (a.scope = 'route' AND a.route_id = l.route_id)
                   OR (a.scope = 'service' AND a.service_id = l.service_id))
       ) AS wheelchair_kinds
FROM rows l
JOIN active.route ro ON ro.route_id = l.route_id
LEFT JOIN posts bp ON bp.location_id = l.boarding_point_id
LEFT JOIN vehicles v USING (trip_id, service_date)
LEFT JOIN rt.vehicle_profile vp ON vp.vehicle_id = v.vehicle_id
WHERE l.expected >= %(start)s AND l.expected < %(until)s
    AND (%(after_expected)s::timestamptz IS NULL
        OR (l.expected, l.scheduled, l.trip_id, l.service_date, l.location_id)
            > (%(after_expected)s, %(after_scheduled)s::timestamptz, %(after_trip)s::text,
               %(after_date)s::date, %(after_location)s::text))
ORDER BY l.expected, l.scheduled, l.trip_id, l.service_date, l.location_id
LIMIT %(limit)s
