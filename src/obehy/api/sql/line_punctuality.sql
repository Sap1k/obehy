-- Punctuality of a line's journeys between two service dates: the departures history recorded
-- for the journey keys of the line's trips (in the preferred namespaces), against their
-- schedule, as on-time share and mean delay overall, by scheduled hour and by stop.
WITH keys AS (
    SELECT DISTINCT k.namespace, k.identifier AS key
    FROM active.trip t
    JOIN active.source_key k ON k.public_id = t.trip_id AND k.entity_kind = 'trip'
    WHERE t.route_id = %(route_id)s AND k.namespace = ANY(%(namespaces)s::text[])
),
latest AS (
    SELECT DISTINCT ON (s.feed, s.key_namespace, s.key, s.service_date)
           s.feed, s.key_namespace, s.key, s.service_date, s.revision
    FROM history.journey_schedule s
    JOIN keys k ON k.namespace = s.key_namespace AND k.key = s.key
    WHERE s.service_date BETWEEN %(since)s AND %(until)s
    ORDER BY s.feed, s.key_namespace, s.key, s.service_date, s.revision DESC
),
events AS (
    SELECT l.feed, l.key_namespace, l.key, l.service_date, c.location_id, c.name,
           control.obehy_instant_fast(l.service_date, c.scheduled_departure) AS scheduled,
           e.event_time AS actual
    FROM latest l
    JOIN history.journey_call c
        ON (c.feed, c.key_namespace, c.key, c.service_date, c.revision)
            = (l.feed, l.key_namespace, l.key, l.service_date, l.revision)
        AND c.passenger_service AND c.scheduled_departure IS NOT NULL
    JOIN history.actual_stop_event e
        ON (e.feed, e.key_namespace, e.key, e.service_date, e.location_id, e.visit_n)
            = (l.feed, l.key_namespace, l.key, l.service_date, c.location_id, c.visit_n)
        AND e.event_type = 'departure' AND NOT e.orphaned
),
delays AS (
    SELECT *, extract(epoch FROM actual - scheduled)::float AS delay_s
    FROM events
),
judged AS (
    SELECT *, (delay_s BETWEEN %(early_s)s AND %(late_s)s)::integer AS on_time
    FROM delays
)
SELECT 'all' AS grouping, NULL::text AS key, NULL::text AS name,
       count(DISTINCT (feed, key_namespace, key, service_date))::integer AS journeys,
       count(*)::integer AS events, avg(on_time)::float AS on_time_share,
       avg(delay_s)::float AS mean_delay_s
FROM judged
UNION ALL
SELECT 'hour', to_char(scheduled AT TIME ZONE 'Europe/Prague', 'HH24'), NULL, NULL,
       count(*)::integer, avg(on_time)::float, avg(delay_s)::float
FROM judged GROUP BY 2
UNION ALL
SELECT 'stop', location_id, min(name), NULL, count(*)::integer, avg(on_time)::float,
       avg(delay_s)::float
FROM judged GROUP BY 2
ORDER BY 1, 2
