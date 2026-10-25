-- Current vehicles of the active release seen since a time, in some states, inside a viewport
-- or one vehicle: one row per vehicle (the worker resolves which vehicle each source report
-- is), every source reporting it, and its profile's traits. A vehicle's trip is the feed trip
-- it is on.
--
-- A vehicle two sources report may be bound differently by each (DÚK and PID disagreeing about
-- its next trip): its trip, state and delay come from its best-ranked report (one standing for
-- its journey, then the state, then the source), so they do not flip with whichever report is
-- fresher; its position comes from the report standing for its journey, else its newest fix (by
-- the fix's own time: the SŽ map's report is heard every poll, its position changes seldom). A
-- viewport lists only vehicles standing for their journey (emit/represent.py): the SŽ map's train
-- and the unit's GPS are one train.
--
-- A search (`text`, its `words`) finds them by line, train number, headsign, fleet number or
-- plate, regardless of case and diacritics, every word somewhere: an exact line or train number
-- ("22", "6819" of "Os 6819") first, then the fleet number or plate, a line it starts, any match;
-- in each, the nearest to `lat`, `lon` first when given.
WITH reports AS (
    SELECT v.*, array_agg(v.source) OVER (PARTITION BY v.vehicle_id) AS sources
    FROM rt.vehicle_state_current v
    WHERE v.release_id = %(release_id)s AND v.last_seen >= %(seen_since)s
        AND (%(vehicle_id)s::text IS NULL OR v.vehicle_id = %(vehicle_id)s)
),
binding AS (
    SELECT DISTINCT ON (r.vehicle_id)
           r.vehicle_id, r.feed, r.source, r.source_vehicle_id, r.status, r.key_namespace, r.key,
           r.service_date, r.trip_id, r.represents, r.sources
    FROM reports r
    ORDER BY r.vehicle_id, r.represents DESC,
        array_position(
            ARRAY['running', 'positioning', 'layover', 'unmatched', 'not_in_service'], r.status
        ),
        r.source, r.source_vehicle_id, r.feed
),
fix AS (
    SELECT DISTINCT ON (r.vehicle_id)
           r.vehicle_id, r.last_seen, r.position_at, r.latitude, r.longitude, r.bearing
    FROM reports r
    ORDER BY r.vehicle_id, r.latitude IS NULL, r.represents DESC, r.position_at DESC NULLS LAST,
        r.last_seen DESC, r.source, r.source_vehicle_id, r.feed
)
SELECT b.vehicle_id, b.feed, b.source, b.source_vehicle_id, b.status, f.last_seen, f.position_at, f.latitude,
       f.longitude, f.bearing, b.trip_id, b.service_date, t.route_id, ro.short_name,
       ro.long_name, ro.mode, ro.color, ro.text_color, t.short_name AS trip_short_name,
       t.headsign, s.delay_s, b.sources, p.low_floor, p.air_conditioning,
       p.wheelchair_accessible, p.manufacturer, p.model, p.operator, p.licence_plate, p.fleet_number,
       m.distance_m
FROM binding b
JOIN fix f USING (vehicle_id)
LEFT JOIN rt.vehicle_profile p USING (vehicle_id)
LEFT JOIN active.trip t ON t.trip_id = b.trip_id
LEFT JOIN active.route ro ON ro.route_id = t.route_id
LEFT JOIN rt.trip_state_current s
    ON (s.feed, s.key_namespace, s.key, s.service_date, s.release_id)
        = (b.feed, b.key_namespace, b.key, b.service_date, %(release_id)s)
-- Folded only for a search: a viewport's thousands of rows need none of it.
CROSS JOIN LATERAL (
    SELECT q, line, train,
           concat_ws(' ', line, train, control.obehy_fold(t.headsign),
                     control.obehy_fold(p.fleet_number), control.obehy_fold(p.licence_plate))
               AS haystack
    FROM (
        SELECT control.obehy_fold(%(text)s::text) AS q,
               control.obehy_fold(ro.short_name) AS line,
               control.obehy_fold(t.short_name) AS train
        WHERE %(text)s::text IS NOT NULL
    ) folded
    UNION ALL
    SELECT NULL, NULL, NULL, NULL
    WHERE %(text)s::text IS NULL
) k
CROSS JOIN LATERAL (
    SELECT CASE
               WHEN k.q IS NULL THEN 0
               WHEN k.line = k.q OR k.train = k.q OR k.train LIKE '%% ' || k.q THEN 0
               WHEN control.obehy_fold(p.fleet_number) = k.q
                   OR control.obehy_fold(p.licence_plate) = k.q THEN 1
               WHEN k.line LIKE k.q || '%%' THEN 2
               ELSE 3
           END AS rank,
           CASE WHEN %(lat)s::double precision IS NULL OR f.latitude IS NULL THEN NULL
                ELSE ST_Distance(
                    ST_SetSRID(ST_MakePoint(f.longitude, f.latitude), 4326)::geography,
                    ST_SetSRID(
                        ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision), 4326
                    )::geography
                ) END AS distance_m
) m
WHERE b.status = ANY(%(states)s::text[])
    AND (%(vehicle_id)s::text IS NOT NULL OR b.represents)
    AND (%(west)s::double precision IS NULL OR (
        f.longitude BETWEEN %(west)s AND %(east)s AND f.latitude BETWEEN %(south)s AND %(north)s
    ))
    AND (k.q IS NULL OR NOT EXISTS (
        SELECT 1 FROM unnest(%(words)s::text[]) AS w
        WHERE k.haystack NOT LIKE '%%' || control.obehy_fold(w) || '%%'
    ))
ORDER BY m.rank, m.distance_m NULLS LAST, ro.short_name, b.vehicle_id
LIMIT %(limit)s
