-- A journey's recorded runs between two service dates: per date the latest schedule revision's
-- passenger calls with the actual arrival and departure (history keeps them self-described),
-- and whether a source cancelled it (and no vehicle was seen running it after all).
WITH latest AS (
    SELECT DISTINCT ON (s.service_date) s.feed, s.service_date, s.revision, s.trip_id
    FROM history.journey_schedule s
    WHERE s.feed IN ('jdf', 'czptt') AND s.key_namespace = %(namespace)s AND s.key = %(key)s
        AND s.service_date BETWEEN %(since)s AND %(until)s
    ORDER BY s.service_date, s.revision DESC
),
numbered AS (
    SELECT l.*, j.journey_id
    FROM latest l
    JOIN history.journey j
        ON (j.feed, j.key_namespace, j.key, j.service_date)
            = (l.feed, %(namespace)s, %(key)s, l.service_date)
)
SELECT l.service_date, l.trip_id, c.ordinal, loc.value AS location_id, stop.value AS name,
       control.obehy_instant_fast(l.service_date, c.scheduled_arrival) AS scheduled_arrival,
       control.obehy_instant_fast(l.service_date, c.scheduled_departure) AS scheduled_departure,
       a.event_time AS actual_arrival, d.event_time AS actual_departure,
       EXISTS (
           SELECT 1 FROM history.journey_cancellation x
           WHERE x.feed = l.feed AND x.key_namespace = %(namespace)s AND x.key = %(key)s
               AND x.service_date = l.service_date AND x.restored_at IS NULL
       ) AS cancelled
FROM numbered l
JOIN history.journey_call c
    ON c.journey_id = l.journey_id AND c.service_date = l.service_date
    AND c.revision = l.revision AND c.passenger_service
JOIN history.term loc ON loc.term_id = c.location_term
JOIN history.term stop ON stop.term_id = c.name_term
LEFT JOIN history.actual_stop_event a
    ON a.journey_id = l.journey_id AND a.service_date = l.service_date
    AND a.location_term = c.location_term AND a.visit_n = c.visit_n AND a.event_type = 'arrival'
LEFT JOIN history.actual_stop_event d
    ON d.journey_id = l.journey_id AND d.service_date = l.service_date
    AND d.location_term = c.location_term AND d.visit_n = c.visit_n
    AND d.event_type = 'departure'
ORDER BY l.service_date DESC, c.ordinal
