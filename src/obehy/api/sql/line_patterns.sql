-- The stop sequences a line's trips run on the date, most used first: passenger calls only,
-- with the shape of one trip of each.
WITH trips AS (
    SELECT t.trip_id, t.headsign, t.shape_id
    FROM active.trip t
    JOIN active.service_date d
        ON d.service_id = t.service_id AND d.service_date = %(service_date)s
    WHERE t.route_id = %(route_id)s
),
sequences AS (
    SELECT t.trip_id, t.headsign, t.shape_id,
           array_agg(c.location_id ORDER BY c.sequence) AS stops
    FROM trips t
    JOIN active.trip_call c ON c.trip_id = t.trip_id AND c.passenger_service
    GROUP BY t.trip_id, t.headsign, t.shape_id
)
SELECT stops, mode() WITHIN GROUP (ORDER BY headsign) AS headsign, count(*)::integer AS trips,
       min(shape_id) AS shape_id
FROM sequences
GROUP BY stops
ORDER BY trips DESC, stops
LIMIT %(limit)s
