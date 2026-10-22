-- rt.alert_target looked up the active loads' source keys in one join with
-- control.publication: the planner could not prune the partitions it would read and hashed every
-- source_key partition of every retained load (seconds per read). Each entity now looks up its
-- key in each active load by the primary key; OFFSET 0 keeps the lookup per entity, so the load
-- IDs prune the partitions as it runs. Same rows as 0015.

CREATE OR REPLACE VIEW rt.alert_target AS
SELECT a.source, a.alert_id, e.ordinal::integer AS ordinal, e.kind, e.service_date,
       k.feed, k.public_id AS target_id
FROM rt.alert a
CROSS JOIN LATERAL ROWS FROM (
    jsonb_to_recordset(a.entities) AS (kind text, namespace text, identifier text, service_date date)
) WITH ORDINALITY AS e (kind, namespace, identifier, service_date, ordinal)
CROSS JOIN control.publication p
CROSS JOIN LATERAL (
    SELECT 'jdf'::text AS feed, j.public_id
    FROM static.source_key j
    WHERE j.load_id = p.jdf_load_id
        AND j.namespace = e.namespace AND j.identifier = e.identifier
        AND j.entity_kind = CASE e.kind WHEN 'stop' THEN 'location' ELSE e.kind END
        AND coalesce(e.service_date, (now() AT TIME ZONE 'Europe/Prague')::date)
            BETWEEN j.valid_from AND j.valid_to
    UNION ALL
    SELECT 'czptt'::text, c.public_id
    FROM static.source_key c
    WHERE c.load_id = p.czptt_load_id
        AND c.namespace = e.namespace AND c.identifier = e.identifier
        AND c.entity_kind = CASE e.kind WHEN 'stop' THEN 'location' ELSE e.kind END
        AND coalesce(e.service_date, (now() AT TIME ZONE 'Europe/Prague')::date)
            BETWEEN c.valid_from AND c.valid_to
    OFFSET 0
) k;
