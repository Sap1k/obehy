-- Alerts informing any of the given routes, stop places (a location, or a boarding point of it)
-- or trips, every alert with `everything`, or one by its source and ID. Current at `at` (not
-- withdrawn, one period not over: future ones included) unless withdrawn or over ones are asked
-- for.
-- Each with what it informs, resolved in the active release; most severe, then latest first.
-- Withdrawn alerts stay in rt.alert until pruned, so only the candidates' entities are resolved.
WITH candidates AS MATERIALIZED (
    SELECT a.*
    FROM rt.alert a
    WHERE (%(withdrawn)s OR a.withdrawn_at IS NULL)
        AND (%(over)s OR jsonb_array_length(a.periods) = 0 OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(a.periods) p
            WHERE p ->> 'end' IS NULL OR (p ->> 'end')::timestamptz > %(at)s
        ))
),
targets AS (
    SELECT t.source, t.alert_id, t.ordinal, t.kind, t.feed, t.target_id, t.service_date,
           CASE WHEN t.kind = 'stop' THEN coalesce(l.parent_location_id, t.target_id) END
               AS stop_place
    FROM candidates a
    JOIN rt.alert_target t USING (source, alert_id)
    LEFT JOIN active.location l ON t.kind = 'stop' AND l.location_id = t.target_id
),
wanted AS (
    SELECT source, alert_id FROM targets
    WHERE (kind = 'route' AND target_id = ANY(%(routes)s::text[]))
        OR (kind = 'stop' AND (target_id = ANY(%(stops)s::text[])
                               OR stop_place = ANY(%(stops)s::text[])))
        OR (kind = 'trip' AND target_id = ANY(%(trips)s::text[]))
    UNION
    SELECT source, alert_id FROM candidates
    WHERE %(everything)s OR (source, alert_id) = (%(source)s, %(alert_id)s)
)
SELECT a.source, a.alert_id, a.cause, a.effect, a.severity, a.periods, a.header, a.description,
       a.url, a.cause_detail, a.effect_detail, a.details, a.first_seen, a.changed_at,
       a.withdrawn_at,
       coalesce((
           SELECT jsonb_agg(jsonb_build_object(
                      'kind', t.kind, 'id', t.target_id, 'feed', t.feed,
                      'service_date', t.service_date, 'stop_place', t.stop_place)
                  ORDER BY t.ordinal, t.target_id)
           FROM targets t WHERE (t.source, t.alert_id) = (a.source, a.alert_id)
       ), '[]'::jsonb) AS informs
FROM candidates a
JOIN wanted USING (source, alert_id)
ORDER BY CASE a.severity WHEN 'SEVERE' THEN 0 WHEN 'WARNING' THEN 1 WHEN 'INFO' THEN 2 ELSE 3 END,
         a.changed_at DESC, a.source, a.alert_id
LIMIT %(limit)s
