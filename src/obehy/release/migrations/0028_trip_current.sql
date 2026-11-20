-- A trip's current realtime, one rule for every reader (boards, a trip, a line's trips): the
-- freshest journey state publishing the trip in a release (should two journeys hold it), with
-- the trip's slice of that journey's calls (a rail run's part, `rt.trip_part_current`).
-- Inlined into the caller.
CREATE FUNCTION rt.trip_current(for_trip text, for_date date, for_release text)
RETURNS TABLE (
    feed text,
    key_namespace text,
    key text,
    lifecycle text,
    delay_s integer,
    off_route boolean,
    stale boolean,
    updated_at timestamptz,
    sources jsonb,
    calls jsonb
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT s.feed, s.key_namespace, s.key, s.lifecycle, s.delay_s, s.off_route, s.stale,
        s.updated_at, s.sources,
        (
            SELECT coalesce(jsonb_agg(e.value ORDER BY e.n), '[]'::jsonb)
            FROM jsonb_array_elements(s.calls) WITH ORDINALITY AS e (value, n)
            WHERE e.n - 1 BETWEEN p.first_call AND p.last_call
        )
    FROM rt.trip_part_current p
    JOIN rt.trip_state_current s USING (feed, key_namespace, key, service_date, release_id)
    WHERE p.trip_id = for_trip AND p.service_date = for_date AND p.release_id = for_release
    ORDER BY s.updated_at DESC
    LIMIT 1
$$;
