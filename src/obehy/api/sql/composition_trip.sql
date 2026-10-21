-- A trip of the active release as its composition is asked: its feed, the journey key its
-- compositions are kept under (the first of `namespaces` it has, as the lookup broker keeps
-- them), whether it runs on the date, and whether `from` is one of its calls.
SELECT CASE WHEN t.load_id = p.jdf_load_id THEN 'jdf' ELSE 'czptt' END AS feed,
       coalesce(k.namespace, 'trip_id') AS key_namespace,
       coalesce(k.identifier, t.trip_id) AS key,
       EXISTS (
           SELECT 1 FROM active.service_date d
           WHERE d.service_id = t.service_id AND d.service_date = %(service_date)s
       ) AS runs,
       (%(from)s::text IS NULL OR EXISTS (
           SELECT 1 FROM active.trip_call c
           WHERE c.trip_id = t.trip_id AND c.location_id = %(from)s
       )) AS from_on_trip
FROM control.publication p
JOIN static.trip t
    ON t.load_id IN (p.jdf_load_id, p.czptt_load_id) AND t.trip_id = %(trip_id)s
LEFT JOIN LATERAL (
    SELECT k.namespace, k.identifier
    FROM active.source_key k
    WHERE k.public_id = t.trip_id AND k.namespace = ANY(%(namespaces)s::text[])
        AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
    ORDER BY array_position(%(namespaces)s::text[], k.namespace), k.identifier
    LIMIT 1
) k ON true
