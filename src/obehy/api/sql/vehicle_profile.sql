-- What is known about a vehicle, as the worker resolved it, and its current source reports.
SELECT p.*,
       coalesce((
           SELECT jsonb_agg(jsonb_build_object(
                      'source', v.source, 'source_vehicle_id', v.source_vehicle_id,
                      'bound_by', v.bound_by, 'state', v.status, 'last_seen', v.last_seen)
                  ORDER BY v.last_seen DESC, v.source, v.source_vehicle_id)
           FROM rt.vehicle_state_current v
           WHERE v.vehicle_id = %(vehicle_id)s AND v.release_id = %(release_id)s
       ), '[]'::jsonb) AS reports
FROM (SELECT %(vehicle_id)s::text AS vehicle_id) wanted
LEFT JOIN rt.vehicle_profile p USING (vehicle_id)
