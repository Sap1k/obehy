-- The active release's station pairing (release/pairing.py): what decided it, the outcomes,
-- every ambiguous station with the candidates it was left between (for an override to settle,
-- jrunify-ext-geodata/stops/station-pairs.csv), and the overrides that name a stop the release
-- does not have or that the rule agrees with anyway.
WITH pairing AS (
    SELECT p.*
    FROM control.station_pairing p
    JOIN control.publication c USING (jdf_load_id, czptt_load_id)
)
SELECT
    (SELECT to_jsonb(r) - 'jdf_load_id' - 'czptt_load_id'
     FROM control.station_pairing_run r
     JOIN control.publication c USING (jdf_load_id, czptt_load_id)) AS run,
    (SELECT jsonb_object_agg(k, n) FROM (
         SELECT outcome || '/' || reason AS k, count(*) AS n FROM pairing GROUP BY 1
     ) o) AS outcomes,
    (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'station_id', p.station_id, 'station', l.name, 'reason', p.reason,
                'candidates', p.candidates) ORDER BY l.name, p.station_id), '[]')
     FROM pairing p
     LEFT JOIN active.location l ON l.location_id = p.station_id
     WHERE p.outcome = 'ambiguous') AS ambiguous,
    (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'station_id', o.station_id, 'stop_id', nullif(o.stop_id, ''),
                'decision', o.decision, 'note', o.note) ORDER BY o.station_id, o.stop_id), '[]')
     FROM ref.station_pair o
     WHERE NOT EXISTS (SELECT 1 FROM pairing p WHERE p.station_id = o.station_id)
         OR (o.stop_id <> '' AND NOT EXISTS (
             SELECT 1 FROM active.location l
             WHERE l.location_id = o.stop_id AND l.kind = 'stop_place'))) AS stale_overrides,
    (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'station_id', o.station_id, 'stop_id', o.stop_id, 'note', o.note)
                ORDER BY o.station_id), '[]')
     FROM ref.station_pair o
     JOIN pairing p ON p.station_id = o.station_id
     WHERE o.decision = 'pair' AND p.rule_stop_id = o.stop_id) AS redundant_overrides
