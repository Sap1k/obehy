-- A journey's recorded runs between two service dates: per date the latest schedule revision's
-- passenger calls with the actual arrival and departure (history keeps them self-described).
WITH latest AS (
    SELECT DISTINCT ON (s.service_date) s.feed, s.service_date, s.revision, s.trip_id
    FROM history.journey_schedule s
    WHERE s.key_namespace = %(namespace)s AND s.key = %(key)s
        AND s.service_date BETWEEN %(since)s AND %(until)s
    ORDER BY s.service_date, s.revision DESC
)
SELECT l.service_date, l.trip_id, c.ordinal, c.location_id, c.name,
       control.obehy_instant_fast(l.service_date, c.scheduled_arrival) AS scheduled_arrival,
       control.obehy_instant_fast(l.service_date, c.scheduled_departure) AS scheduled_departure,
       a.event_time AS actual_arrival, d.event_time AS actual_departure
FROM latest l
JOIN history.journey_call c
    ON c.feed = l.feed AND c.key_namespace = %(namespace)s AND c.key = %(key)s
    AND c.service_date = l.service_date AND c.revision = l.revision AND c.passenger_service
LEFT JOIN history.actual_stop_event a
    ON a.feed = l.feed AND a.key_namespace = %(namespace)s AND a.key = %(key)s
    AND a.service_date = l.service_date AND a.location_id = c.location_id
    AND a.visit_n = c.visit_n AND a.event_type = 'arrival'
LEFT JOIN history.actual_stop_event d
    ON d.feed = l.feed AND d.key_namespace = %(namespace)s AND d.key = %(key)s
    AND d.service_date = l.service_date AND d.location_id = c.location_id
    AND d.visit_n = c.visit_n AND d.event_type = 'departure'
ORDER BY l.service_date DESC, c.ordinal
