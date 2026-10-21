-- Every alert of the last window (current or withdrawn since), with each entity as the source
-- named it and what it resolved to in the active release (none: unresolved).
SELECT a.source, a.alert_id, a.channel, a.effect, a.header, a.first_seen, a.last_seen,
       a.changed_at, a.withdrawn_at,
       jsonb_agg(e.value || jsonb_build_object('targets', coalesce((
           SELECT jsonb_agg(jsonb_build_object('feed', t.feed, 'id', t.target_id)
                            ORDER BY t.feed, t.target_id)
           FROM rt.alert_target t
           WHERE (t.source, t.alert_id, t.ordinal) = (a.source, a.alert_id, e.ordinal)
       ), '[]'::jsonb)) ORDER BY e.ordinal) FILTER (WHERE e.value IS NOT NULL) AS entities
FROM rt.alert a
LEFT JOIN LATERAL jsonb_array_elements(a.entities) WITH ORDINALITY AS e (value, ordinal) ON true
WHERE a.withdrawn_at IS NULL OR a.withdrawn_at >= %(since)s
GROUP BY a.source, a.alert_id
ORDER BY a.source, a.alert_id
LIMIT %(limit)s
