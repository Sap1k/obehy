-- The active release: its release.json, its packages and the loads behind it, and the latest
-- activations and rollbacks.
SELECT p.run_id, p.activated_at, r.completed_at, r.gvd_year, r.registered_at, r.release_json,
       (SELECT jsonb_agg(jsonb_build_object(
                   'package', k.package, 'feed_version', k.feed_version,
                   'serving_schema_version', k.serving_schema_version) ORDER BY k.package)
        FROM control.package k WHERE k.run_id = p.run_id) AS packages,
       (SELECT jsonb_agg(jsonb_build_object(
                   'load_id', l.load_id, 'package', l.package, 'status', l.status,
                   'started_at', l.started_at, 'finished_at', l.finished_at,
                   'row_counts', l.row_counts, 'warnings', l.warnings) ORDER BY l.load_id)
        FROM control.load l WHERE l.load_id IN (p.jdf_load_id, p.czptt_load_id)) AS loads,
       (SELECT jsonb_agg(to_jsonb(h) ORDER BY h.seq DESC)
        FROM (SELECT * FROM control.publication_history ORDER BY seq DESC LIMIT %(limit)s) h)
           AS history
FROM control.publication p
JOIN control.release r USING (run_id)
