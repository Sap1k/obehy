-- Alerts from any source (BASE_PLAN.md section 25, realtime/alerts.py). Each poll of an alert
-- channel states its whole current set: alerts in it are upserted (changed_at when their
-- content changes), alerts missing from it are withdrawn. Withdrawn alerts are deleted by the
-- nightly job after the raw archive window.
CREATE TABLE rt.alert (
    source text NOT NULL,
    alert_id text NOT NULL,
    channel text NOT NULL,
    cause text NOT NULL,
    effect text NOT NULL,
    severity text NOT NULL,
    periods jsonb NOT NULL,        -- [{start, end}], instants; null for open
    header jsonb NOT NULL,         -- {language: text}
    description jsonb NOT NULL,
    url jsonb NOT NULL,
    cause_detail jsonb NOT NULL,
    effect_detail jsonb NOT NULL,
    -- What it informs, as the source names it: [{kind: route | stop | trip, namespace,
    -- identifier, service_date}], an identifier in a source key namespace.
    entities jsonb NOT NULL,
    details jsonb NOT NULL,        -- source-specific
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    changed_at timestamptz NOT NULL,
    withdrawn_at timestamptz,
    PRIMARY KEY (source, alert_id)
);
CREATE INDEX alert_current ON rt.alert (source, channel) WHERE withdrawn_at IS NULL;

-- Alerts' entities resolved against the active release: the feed (the load the key belongs to)
-- and our ID of the route, location or trip. A key is taken valid on the trip's service date,
-- else on today's (Europe/Prague). An entity no key names has no row.
CREATE VIEW rt.alert_target AS
SELECT a.source, a.alert_id, e.ordinal::integer AS ordinal, e.kind, e.service_date,
       CASE WHEN k.load_id = p.jdf_load_id THEN 'jdf' ELSE 'czptt' END AS feed,
       k.public_id AS target_id
FROM rt.alert a
CROSS JOIN LATERAL ROWS FROM (
    jsonb_to_recordset(a.entities) AS (kind text, namespace text, identifier text, service_date date)
) WITH ORDINALITY AS e (kind, namespace, identifier, service_date, ordinal)
CROSS JOIN control.publication p
JOIN static.source_key k
    ON k.load_id IN (p.jdf_load_id, p.czptt_load_id)
    AND k.namespace = e.namespace AND k.identifier = e.identifier
    AND k.entity_kind = CASE e.kind WHEN 'stop' THEN 'location' ELSE e.kind END
    AND coalesce(e.service_date, (now() AT TIME ZONE 'Europe/Prague')::date)
        BETWEEN k.valid_from AND k.valid_to;
