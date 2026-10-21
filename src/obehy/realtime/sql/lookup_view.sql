-- What the lookup broker knows of a trip on a date (realtime/compositions.py, TripView): its
-- calls in order with their location keys (SR70) and whether the train has left them (the
-- journey's current state, through the part the trip publishes), and the trip's keys, in the
-- namespaces lookups plan with. No row: the trip is not in the active release.
WITH trip AS (
    SELECT t.trip_id,
           CASE WHEN t.load_id = p.jdf_load_id THEN 'jdf' ELSE 'czptt' END AS feed,
           p.run_id
    FROM control.publication p
    JOIN static.trip t
        ON t.load_id IN (p.jdf_load_id, p.czptt_load_id) AND t.trip_id = %(trip_id)s
),
part AS (
    SELECT pc.first_call, s.calls
    FROM rt.trip_part_current pc
    JOIN rt.trip_state_current s USING (feed, key_namespace, key, service_date, release_id)
    JOIN trip ON trip.run_id = pc.release_id
    WHERE pc.trip_id = %(trip_id)s AND pc.service_date = %(service_date)s
    ORDER BY s.updated_at DESC
    LIMIT 1
),
calls AS (
    SELECT (row_number() OVER (ORDER BY c.sequence) - 1)::integer AS ordinal,
           c.location_id,
           (row_number() OVER (PARTITION BY c.location_id ORDER BY c.sequence))::integer
               AS visit_n,
           c.passenger_service AS passenger
    FROM active.trip_call c
    WHERE c.trip_id = %(trip_id)s
)
SELECT trip.feed,
       -- One identifier per namespace, the least (the last of equal keys wins the object).
       (SELECT coalesce(jsonb_object_agg(k.namespace, k.identifier ORDER BY k.identifier DESC),
                        '{}')
        FROM active.source_key k
        WHERE k.public_id = trip.trip_id AND k.namespace = ANY(%(trip_namespaces)s::text[])
            AND %(service_date)s BETWEEN k.valid_from AND k.valid_to) AS keys,
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
                    'ordinal', c.ordinal,
                    'location_id', c.location_id,
                    'visit_n', c.visit_n,
                    'passenger', c.passenger,
                    'left', coalesce(
                        live.call ->> 'status' = 'actual'
                        AND coalesce(live.call ->> 'estimated_departure',
                                     live.call ->> 'estimated_arrival')::timestamptz
                            <= statement_timestamp(),
                        false),
                    'keys', (SELECT coalesce(jsonb_object_agg(k.namespace, k.identifier), '{}')
                             FROM active.source_key k
                             WHERE k.public_id = c.location_id
                                 AND k.namespace = ANY(%(location_namespaces)s::text[])
                                 AND %(service_date)s BETWEEN k.valid_from AND k.valid_to))
                ORDER BY c.ordinal), '[]')
        FROM calls c
        LEFT JOIN LATERAL (
            SELECT part.calls -> (part.first_call + c.ordinal) AS call FROM part
        ) live ON true) AS calls
FROM trip
