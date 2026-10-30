-- The journey a trip ID's runs are recorded under. Of its keys in the active release (in the
-- preferred namespaces), the one history recorded on most days between the dates (each source
-- records a journey by its own key: PID's by pid:gtfs_trip_id), else the preferred one. A trip
-- ID of an earlier timetable version: the key history recorded it under.
WITH keys AS (
    SELECT DISTINCT k.namespace, k.identifier AS key,
           array_position(%(namespaces)s::text[], k.namespace) AS preference
    FROM active.source_key k
    WHERE k.public_id = %(trip_id)s AND k.entity_kind = 'trip'
        AND k.namespace = ANY(%(namespaces)s::text[])
),
recorded AS (
    SELECT k.namespace, k.key, k.preference,
           (
               SELECT count(DISTINCT s.service_date)
               FROM history.journey_schedule s
               WHERE s.feed IN ('jdf', 'czptt') AND s.key_namespace = k.namespace
                   AND s.key = k.key AND s.service_date BETWEEN %(since)s AND %(until)s
           ) AS days
    FROM keys k
)
(
    SELECT namespace, key, 0 AS found_in
    FROM recorded
    ORDER BY days DESC, preference, key
    LIMIT 1
)
UNION ALL
(
    SELECT key_namespace, key, 1
    FROM history.journey_schedule
    WHERE trip_id = %(trip_id)s AND service_date BETWEEN %(since)s AND %(until)s
    ORDER BY service_date DESC, revision DESC
    LIMIT 1
)
ORDER BY found_in
LIMIT 1
