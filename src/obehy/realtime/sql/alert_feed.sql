-- Per feed, the current alerts (not withdrawn) with the entities resolved in that feed, in
-- order: one row per alert and feed, by source and ID. An alert with no entity resolved in a
-- feed is not that feed's.
SELECT t.feed, a.source, a.alert_id, a.cause, a.effect, a.severity, a.periods, a.header,
       a.description, a.url, a.cause_detail, a.effect_detail, a.entities, a.details,
       jsonb_agg(jsonb_build_object('kind', t.kind, 'id', t.target_id,
                                    'service_date', t.service_date)
                 ORDER BY t.ordinal, t.target_id) AS targets
FROM rt.alert a
JOIN rt.alert_target t USING (source, alert_id)
WHERE a.withdrawn_at IS NULL
GROUP BY t.feed, a.source, a.alert_id
ORDER BY t.feed, a.source, a.alert_id
