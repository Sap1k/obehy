-- The registry's datasets with what each holds, then the resolved profiles: how many, how
-- many registered, and the vehicles whose sources or datasets disagree on an attribute.
(SELECT 'dataset' AS row_kind, to_jsonb(d) || jsonb_build_object(
        'vehicles', (SELECT count(*) FROM ref.vehicle v WHERE v.dataset = d.dataset),
        'models', (SELECT count(*) FROM ref.vehicle_model m WHERE m.dataset = d.dataset),
        'source_keys', (SELECT count(*) FROM ref.vehicle_key k
                        WHERE k.dataset = d.dataset AND k.source IS NOT NULL),
        'identifier_keys', (SELECT jsonb_object_agg(scheme, n) FROM (
            SELECT k.scheme, count(*) AS n FROM ref.vehicle_key k
            WHERE k.dataset = d.dataset AND k.scheme IS NOT NULL GROUP BY k.scheme) s),
        'attributes', (SELECT count(*) FROM ref.vehicle_attribute a
                       WHERE a.dataset = d.dataset)) AS body
 FROM ref.dataset d
 ORDER BY d.priority, d.dataset)
UNION ALL
SELECT 'profiles', jsonb_build_object(
    'vehicles', count(*),
    'registered', count(*) FILTER (WHERE registered),
    'unregistered_with_plate', count(*) FILTER (WHERE NOT registered AND licence_plate IS NOT NULL))
FROM rt.vehicle_profile
UNION ALL
(SELECT 'conflict', jsonb_build_object('vehicle_id', p.vehicle_id, 'attributes',
        jsonb_agg(a->'attribute' ORDER BY a->>'attribute'))
 FROM rt.vehicle_profile p
 CROSS JOIN LATERAL jsonb_array_elements(p.attributes) a
 WHERE (a->>'conflict')::boolean
 GROUP BY p.vehicle_id
 ORDER BY p.vehicle_id
 LIMIT %(limit)s)
