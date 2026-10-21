-- What each source contributes now: its vehicles seen since a time (on trips, bound to a
-- registered or co-located vehicle) and the journeys whose realtime it feeds or leads.
WITH vehicles AS (
    SELECT source, count(*)::integer AS vehicles,
           count(*) FILTER (WHERE trip_id IS NOT NULL)::integer AS on_trips,
           count(*) FILTER (WHERE bound_by IN ('source', 'identifier'))::integer AS registered,
           count(*) FILTER (WHERE bound_by = 'co_located')::integer AS co_located,
           max(last_seen) AS last_seen
    FROM rt.vehicle_state_current
    WHERE release_id = %(release_id)s AND last_seen >= %(since)s
    GROUP BY source
),
journeys AS (
    SELECT e->>'source' AS source, count(*)::integer AS journeys,
           count(*) FILTER (WHERE (e->>'leads_position')::boolean)::integer AS leads
    FROM rt.trip_state_current s
    CROSS JOIN LATERAL jsonb_array_elements(s.sources) e
    WHERE s.release_id = %(release_id)s
    GROUP BY 1
)
SELECT source, coalesce(v.vehicles, 0) AS vehicles, coalesce(v.on_trips, 0) AS on_trips,
       coalesce(v.registered, 0) AS registered, coalesce(v.co_located, 0) AS co_located,
       v.last_seen, coalesce(j.journeys, 0) AS journeys, coalesce(j.leads, 0) AS leads
FROM vehicles v
FULL JOIN journeys j USING (source)
ORDER BY source
