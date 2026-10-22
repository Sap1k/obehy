-- The project API's hot paths (src/obehy/api): a board finds a stop's calls by their scheduled
-- time, not every call of the release at the stop; the board and a trip find a trip's vehicles
-- by trip. Loads build the static indexes on each partition before ATTACH (ddl.INDEXES); here
-- they reach the partitions already loaded, which takes a few minutes on a national release.

CREATE INDEX trip_call_location_departure ON static.trip_call
    USING btree (load_id, location_id, scheduled_departure);
CREATE INDEX trip_call_location_arrival ON static.trip_call
    USING btree (load_id, location_id, scheduled_arrival);
ANALYZE static.trip_call;

CREATE INDEX vehicle_state_trip ON rt.vehicle_state_current (trip_id, service_date);

-- The worker rewrites the current tables every tick: free space on each page keeps the updates
-- HOT, and vacuum runs after a few hundred dead rows instead of a fifth of the table.
ALTER TABLE rt.vehicle_state_current SET (
    fillfactor = 50, autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 500,
    autovacuum_vacuum_cost_delay = 0, autovacuum_analyze_scale_factor = 0.05
);
ALTER TABLE rt.trip_state_current SET (
    fillfactor = 50, autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 500,
    autovacuum_vacuum_cost_delay = 0, autovacuum_analyze_scale_factor = 0.05,
    toast.autovacuum_vacuum_scale_factor = 0, toast.autovacuum_vacuum_threshold = 500,
    toast.autovacuum_vacuum_cost_delay = 0
);
ALTER TABLE rt.trip_part_current SET (
    fillfactor = 50, autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 500,
    autovacuum_vacuum_cost_delay = 0, autovacuum_analyze_scale_factor = 0.05
);
ALTER TABLE rt.vehicle_profile SET (
    fillfactor = 70, autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 500,
    autovacuum_vacuum_cost_delay = 0
);

-- Query statistics for tuning (deploy/compose.yaml preloads the library). Only a superuser may
-- create the extension; elsewhere it is left to the administrator.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements')
        AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
    END IF;
END
$$;
