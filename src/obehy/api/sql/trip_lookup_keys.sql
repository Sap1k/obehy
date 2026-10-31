-- What a lookup's `applies` is checked against: the trip's feed and its keys on the date in
-- the namespaces the lookups name (namespace -> the first identifier).
SELECT CASE WHEN t.load_id = p.jdf_load_id THEN 'jdf' ELSE 'czptt' END AS feed,
       coalesce((
           SELECT jsonb_object_agg(k.namespace, k.identifier)
           FROM (
               SELECT DISTINCT ON (k.namespace) k.namespace, k.identifier
               FROM active.source_key k
               WHERE k.public_id = t.trip_id AND k.entity_kind = 'trip'
                   AND k.namespace = ANY(%(namespaces)s::text[])
                   AND %(service_date)s BETWEEN k.valid_from AND k.valid_to
               ORDER BY k.namespace, k.identifier
           ) k
       ), '{}'::jsonb) AS keys
FROM control.publication p
JOIN static.trip t
    ON t.load_id IN (p.jdf_load_id, p.czptt_load_id) AND t.trip_id = %(trip_id)s
