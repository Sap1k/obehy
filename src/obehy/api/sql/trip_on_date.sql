-- A trip of the active release on a date as its journeys know it (api/resolve.py): its feed,
-- whether it runs on the date, and every journey key it has on the date, the policy's journey
-- namespaces first in their order of preference, then the rest.
SELECT CASE WHEN t.load_id = p.jdf_load_id THEN 'jdf' ELSE 'czptt' END AS feed,
       EXISTS (
           SELECT 1 FROM active.service_date d
           WHERE d.service_id = t.service_id AND d.service_date = %(service_date)s
       ) AS runs,
       coalesce((
           SELECT jsonb_agg(
                      jsonb_build_object('namespace', k.namespace, 'key', k.identifier)
                      ORDER BY array_position(%(namespaces)s::text[], k.namespace) NULLS LAST,
                          k.namespace, k.identifier
                  )
           FROM active.source_key k
           WHERE k.public_id = t.trip_id AND k.entity_kind = 'trip'
               AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
       ), '[]'::jsonb) AS keys
FROM control.publication p
JOIN static.trip t
    ON t.load_id IN (p.jdf_load_id, p.czptt_load_id) AND t.trip_id = %(trip_id)s
