-- The lines a source's vehicles worked on a date, as `circulation_scopes.sql` reads their
-- journeys, in distinct vehicles: the lines the day's oběhy can be scoped by. A line the active
-- release still has is shown as it is now; else by the name it ran under.
WITH physical AS (
    SELECT *
    FROM unnest(%(physical_sources)s::text[], %(physical_modes)s::text[],
        %(physical)s::boolean[]) AS p (source, mode, physical)
),
worked AS (
    SELECT a.source_vehicle_id, s.route_id, s.mode, s.agency_id, s.route_name
    FROM history.vehicle_assignment a
    JOIN history.journey j USING (feed, key_namespace, key, service_date)
    JOIN history.journey_schedule s
        ON (s.feed, s.key_namespace, s.key, s.service_date, s.revision)
            = (j.feed, j.key_namespace, j.key, j.service_date, j.latest_revision)
    LEFT JOIN physical pm ON pm.source = a.source AND pm.mode = s.mode
    LEFT JOIN physical pd ON pd.source = a.source AND pd.mode = 'default'
    WHERE a.source = %(source)s AND a.service_date = %(service_date)s
        AND s.route_id IS NOT NULL AND coalesce(pm.physical, pd.physical, true)
),
lines AS (
    SELECT route_id, max(route_name) AS route_name, max(mode) AS mode,
           max(agency_id) AS agency_id, count(DISTINCT source_vehicle_id) AS vehicles
    FROM worked
    GROUP BY route_id
)
SELECT l.route_id, coalesce(r.short_name, l.route_name) AS short_name, r.long_name,
       coalesce(r.mode, l.mode) AS mode, r.color, r.text_color,
       coalesce(ag.name, l.agency_id) AS agency, l.vehicles
FROM lines l
LEFT JOIN active.route r ON r.route_id = l.route_id
LEFT JOIN active.agency ag ON ag.agency_id = coalesce(r.agency_id, l.agency_id)
ORDER BY r.sort_order NULLS LAST, length(coalesce(r.short_name, l.route_name)),
    coalesce(r.short_name, l.route_name), l.route_id
