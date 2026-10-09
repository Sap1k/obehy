-- Shared time helper; the SQL twin of obehy.realtime.times.ServiceTime.instant
-- (BASE_PLAN.md section 19.3). Schedule times are wall-clock seconds from local midnight of the
-- service date. A repeated autumn hour takes its first occurrence; a time in the skipped spring
-- hour is read with the pre-change offset. Europe/Prague only ever uses +01 and +02, so the
-- readings are found explicitly instead of relying on AT TIME ZONE's choice for ambiguous times.
CREATE FUNCTION control.obehy_instant(service_date date, seconds integer)
RETURNS timestamptz
LANGUAGE sql
STABLE
PARALLEL SAFE
RETURN (
    WITH wall AS (
        SELECT service_date + make_interval(secs => seconds) AS value
    ),
    reading AS (
        SELECT (wall.value - offset_value) AT TIME ZONE 'UTC' AS value
        FROM wall, (VALUES (interval '2 hours'), (interval '1 hour')) AS offsets (offset_value)
        WHERE ((wall.value - offset_value) AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Prague'
            = wall.value
    )
    SELECT coalesce(
        (SELECT min(value) FROM reading),
        (SELECT (value - interval '1 hour') AT TIME ZONE 'UTC' FROM wall)
    )
);
