-- A journey's line as it ran (BASE_PLAN.md sections 22 and 29): the route, mode and company of
-- the trip its schedule snapshot names, copied with the snapshot so a past day keeps them after
-- the release that had the trip is gone. Opaque copies, not references: history still never
-- references static.*. With the calls already kept they complete the journey's fingerprint
-- (line, origin, destination, departure).
ALTER TABLE history.journey_schedule
    ADD COLUMN route_id text,
    ADD COLUMN mode text,
    ADD COLUMN agency_id text;

CREATE INDEX journey_schedule_route ON history.journey_schedule (route_id, service_date);

-- The days kept so far, from the active release: the trip the snapshot names when it is still
-- there; else the trips its journey key names, whatever their validity, when exactly one route
-- among those whose calls match the snapshot's (its first passenger call's stop and departure,
-- its last's stop and arrival). A reused number never pins a wrong line; the rest stay null.
UPDATE history.journey_schedule s
SET route_id = r.route_id, mode = r.mode, agency_id = r.agency_id
FROM active.trip t
JOIN active.route r ON r.route_id = t.route_id
WHERE t.trip_id = s.trip_id;

WITH ends AS (
    SELECT s.feed, s.key_namespace, s.key, s.service_date, s.revision,
           (array_agg(loc.value ORDER BY c.ordinal))[1] AS origin,
           (array_agg(c.scheduled_departure ORDER BY c.ordinal))[1] AS departure,
           (array_agg(loc.value ORDER BY c.ordinal DESC))[1] AS destination,
           (array_agg(c.scheduled_arrival ORDER BY c.ordinal DESC))[1] AS arrival
    FROM history.journey_schedule s
    JOIN history.journey j USING (feed, key_namespace, key, service_date)
    JOIN history.journey_call c
        ON c.journey_id = j.journey_id AND c.service_date = s.service_date
        AND c.revision = s.revision AND c.passenger_service
    JOIN history.term loc ON loc.term_id = c.location_term
    WHERE s.route_id IS NULL
    GROUP BY s.feed, s.key_namespace, s.key, s.service_date, s.revision
),
matched AS (
    SELECT e.feed, e.key_namespace, e.key, e.service_date, e.revision,
           min(r.route_id) AS route_id, min(r.mode) AS mode, min(r.agency_id) AS agency_id
    FROM ends e
    JOIN active.source_key k
        ON k.entity_kind = 'trip' AND (k.namespace, k.identifier) = (e.key_namespace, e.key)
    JOIN active.trip t ON t.trip_id = k.public_id
    JOIN active.route r ON r.route_id = t.route_id
    WHERE EXISTS (
            SELECT FROM active.trip_call o
            WHERE o.trip_id = t.trip_id AND o.passenger_service
                AND o.location_id = e.origin
                AND o.scheduled_departure IS NOT DISTINCT FROM e.departure
        )
        AND EXISTS (
            SELECT FROM active.trip_call d
            WHERE d.trip_id = t.trip_id AND d.passenger_service
                AND d.location_id = e.destination
                AND d.scheduled_arrival IS NOT DISTINCT FROM e.arrival
        )
    GROUP BY e.feed, e.key_namespace, e.key, e.service_date, e.revision
    HAVING count(DISTINCT r.route_id) = 1
)
UPDATE history.journey_schedule s
SET route_id = m.route_id, mode = m.mode, agency_id = m.agency_id
FROM matched m
WHERE (s.feed, s.key_namespace, s.key, s.service_date, s.revision)
    = (m.feed, m.key_namespace, m.key, m.service_date, m.revision);
