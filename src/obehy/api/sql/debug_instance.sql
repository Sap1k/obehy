-- A journey's current state, its published parts, the vehicle reports on it and its
-- observations since a time.
(SELECT 'state' AS row_kind, to_jsonb(s) AS body
 FROM rt.trip_state_current s
 WHERE (s.feed, s.key_namespace, s.key, s.service_date)
     = (%(feed)s, %(namespace)s, %(key)s, %(service_date)s))
UNION ALL
(SELECT 'part', to_jsonb(p)
 FROM rt.trip_part_current p
 WHERE (p.feed, p.key_namespace, p.key, p.service_date)
     = (%(feed)s, %(namespace)s, %(key)s, %(service_date)s))
UNION ALL
(SELECT 'vehicle', to_jsonb(v)
 FROM rt.vehicle_state_current v
 WHERE (v.feed, v.key_namespace, v.key, v.service_date)
     = (%(feed)s, %(namespace)s, %(key)s, %(service_date)s))
UNION ALL
(SELECT 'observation', jsonb_build_object(
        'source', o.source, 'channel', o.channel, 'observed_at', o.observed_at,
        'reason', o.reason, 'facts', o.facts, 'release_id', o.release_id,
        'received_at', o.received_at)
 FROM rt.observation o
 WHERE o.received_at >= %(since)s AND o.feed = %(feed)s
     AND o.journey_namespace = %(namespace)s AND o.journey_key = %(key)s
     AND o.service_date = %(service_date)s
 ORDER BY o.received_at DESC
 LIMIT %(limit)s)
