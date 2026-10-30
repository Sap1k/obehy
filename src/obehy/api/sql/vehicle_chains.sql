-- The working days of some source vehicles between two service dates, by the nightly job's
-- rule (history.vehicle_chains), with the registry vehicle each was (only %(vehicle_id)s's, when
-- given). Each journey as history recorded it (its latest schedule revision: trip ID, line
-- name, headsign, first and last passenger call), with the line of the active release when
-- the trip is in it. One row per journey, chains in order.
WITH chains AS (
    SELECT c.*, history.vehicle_of(c.source, c.source_vehicle_id, c.service_date) AS vehicle_id
    FROM generate_series(%(since)s::date, %(until)s::date, interval '1 day') AS d (day),
    LATERAL history.vehicle_chains(
        d.day::date, %(gap_s)s::integer, %(sources)s::text[], %(source_vehicle_ids)s::text[]
    ) c
),
journeys AS (
    SELECT c.vehicle_id, c.feed, c.source, c.source_vehicle_id, c.service_date AS day, c.seq,
           c.first_seen AS chain_first_seen, c.last_seen AS chain_last_seen, j.ord,
           j.value ->> 'key_namespace' AS key_namespace, j.value ->> 'key' AS key,
           (j.value ->> 'service_date')::date AS service_date,
           (j.value ->> 'first_seen')::timestamptz AS first_seen,
           (j.value ->> 'last_seen')::timestamptz AS last_seen
    FROM chains c,
    LATERAL jsonb_array_elements(c.journeys) WITH ORDINALITY AS j (value, ord)
    WHERE %(vehicle_id)s::text IS NULL OR c.vehicle_id = %(vehicle_id)s
)
SELECT j.vehicle_id, j.feed, j.source, j.source_vehicle_id, j.day, j.seq, j.chain_first_seen,
       j.chain_last_seen, j.key_namespace, j.key, j.service_date, j.first_seen, j.last_seen,
       s.trip_id, s.route_name, s.headsign,
       ends.origin_id, ends.origin, ends.departure, ends.destination_id, ends.destination,
       ends.arrival,
       t.route_id, r.short_name, r.long_name, r.mode, r.color, r.text_color
FROM journeys j
LEFT JOIN LATERAL (
    SELECT x.trip_id, x.route_name, x.headsign, x.revision
    FROM history.journey_schedule x
    WHERE (x.feed, x.key_namespace, x.key, x.service_date)
        = (j.feed, j.key_namespace, j.key, j.service_date)
    ORDER BY x.revision DESC
    LIMIT 1
) s ON true
LEFT JOIN history.journey hj
    ON (hj.feed, hj.key_namespace, hj.key, hj.service_date)
        = (j.feed, j.key_namespace, j.key, j.service_date)
LEFT JOIN LATERAL (
    SELECT (array_agg(loc.value ORDER BY c.ordinal))[1] AS origin_id,
           (array_agg(name.value ORDER BY c.ordinal))[1] AS origin,
           control.obehy_instant_fast(
               j.service_date, (array_agg(c.scheduled_departure ORDER BY c.ordinal))[1]
           ) AS departure,
           (array_agg(loc.value ORDER BY c.ordinal DESC))[1] AS destination_id,
           (array_agg(name.value ORDER BY c.ordinal DESC))[1] AS destination,
           control.obehy_instant_fast(
               j.service_date, (array_agg(c.scheduled_arrival ORDER BY c.ordinal DESC))[1]
           ) AS arrival
    FROM history.journey_call c
    JOIN history.term loc ON loc.term_id = c.location_term
    JOIN history.term name ON name.term_id = c.name_term
    WHERE c.journey_id = hj.journey_id AND c.service_date = j.service_date
        AND c.revision = s.revision AND c.passenger_service
) ends ON true
LEFT JOIN active.trip t ON t.trip_id = s.trip_id
LEFT JOIN active.route r ON r.route_id = t.route_id
ORDER BY j.day DESC, j.chain_first_seen, j.vehicle_id, j.source, j.source_vehicle_id, j.ord
