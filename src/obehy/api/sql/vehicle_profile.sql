-- What is known about a vehicle, as the worker resolved it, and its current source reports
-- (each with whether its ID is a vehicle on the mode of the trip it is on).
SELECT p.*,
       coalesce((
           SELECT jsonb_agg(jsonb_build_object(
                      'source', v.source, 'source_vehicle_id', v.source_vehicle_id,
                      'bound_by', v.bound_by, 'state', v.status, 'last_seen', v.last_seen,
                      'physical', rt.is_physical(v.source, ro.mode))
                  ORDER BY v.last_seen DESC, v.source, v.source_vehicle_id)
           FROM rt.vehicle_state_current v
           LEFT JOIN active.trip t ON t.trip_id = v.trip_id
           LEFT JOIN active.route ro ON ro.route_id = t.route_id
           WHERE v.vehicle_id = %(vehicle_id)s AND v.release_id = %(release_id)s
       ), '[]'::jsonb) AS reports
FROM (SELECT %(vehicle_id)s::text AS vehicle_id) wanted
LEFT JOIN rt.vehicle_profile p USING (vehicle_id)
