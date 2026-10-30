-- The source vehicles that may be this vehicle on some date: its registry's source-ID keys,
-- the reports that are it now, and the report its ID is Oběhy's own form of
-- (`<source>:<source_vehicle_id>`, as history.vehicle_of writes it; a source's name has no
-- colon). Which of them it was on a date is history.vehicle_of's to say.
SELECT DISTINCT source, source_vehicle_id
FROM (
    SELECT k.source, k.source_vehicle_id
    FROM ref.vehicle_key k
    WHERE k.vehicle_id = %(vehicle_id)s AND k.source IS NOT NULL
    UNION ALL
    SELECT v.source, v.source_vehicle_id
    FROM rt.vehicle_state_current v
    WHERE v.vehicle_id = %(vehicle_id)s
    UNION ALL
    SELECT split_part(%(vehicle_id)s, ':', 1),
           substr(%(vehicle_id)s, strpos(%(vehicle_id)s, ':') + 1)
    WHERE strpos(%(vehicle_id)s, ':') > 1
) s
ORDER BY source, source_vehicle_id
