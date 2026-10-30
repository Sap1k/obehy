-- The trips of a trip's static block (a feed's `block_key`) that run on the date, in order,
-- with their first and last passenger calls. CZPTT's block is the train path, already the
-- run's parts, so only other feeds' blocks are a circulation.
WITH block AS (
    SELECT t.block_key
    FROM active.trip t
    WHERE t.trip_id = %(trip_id)s AND t.block_key IS NOT NULL
        AND t.block_key NOT LIKE 'czptt:%%'
)
SELECT t.trip_id, t.headsign, t.short_name AS trip_short_name, t.route_id, r.short_name,
       r.long_name, r.mode, r.color, r.text_color,
       first.location_id AS origin_id, first.name AS origin,
       control.obehy_instant_fast(
           %(service_date)s, coalesce(first.scheduled_departure, first.scheduled_arrival)
       ) AS departure,
       last.location_id AS destination_id, last.name AS destination,
       control.obehy_instant_fast(
           %(service_date)s, coalesce(last.scheduled_arrival, last.scheduled_departure)
       ) AS arrival
FROM block b
JOIN active.trip t ON t.block_key = b.block_key
JOIN active.service_date d ON d.service_id = t.service_id AND d.service_date = %(service_date)s
JOIN active.route r ON r.route_id = t.route_id
CROSS JOIN LATERAL (
    SELECT c.location_id, l.name, c.scheduled_arrival, c.scheduled_departure
    FROM active.trip_call c JOIN active.location l ON l.location_id = c.location_id
    WHERE c.trip_id = t.trip_id AND c.passenger_service
    ORDER BY c.sequence LIMIT 1
) first
CROSS JOIN LATERAL (
    SELECT c.location_id, l.name, c.scheduled_arrival, c.scheduled_departure
    FROM active.trip_call c JOIN active.location l ON l.location_id = c.location_id
    WHERE c.trip_id = t.trip_id AND c.passenger_service
    ORDER BY c.sequence DESC LIMIT 1
) last
ORDER BY departure, t.trip_id
