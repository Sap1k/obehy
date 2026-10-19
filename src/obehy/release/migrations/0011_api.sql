-- The project API (src/obehy/api, BASE_PLAN.md section 30): stop search by name regardless of
-- case and diacritics, the features of a trip, its route and its service, the current state of
-- a feed trip, and an older release's trip ID by history. Loads build the static indexes on each
-- partition before ATTACH (ddl.INDEXES); here they reach the partitions already loaded.

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- unaccent() is only STABLE (its dictionary could change); naming the dictionary makes the
-- folding fixed, so it can be indexed.
CREATE FUNCTION control.obehy_fold(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
STRICT
RETURN lower(public.unaccent('public.unaccent'::regdictionary, value));

-- control.obehy_instant is exact but costs a plan per call; a departure board turns thousands
-- of schedule times into instants. Prague changes offset only between 02:00 and 03:00 local
-- wall time, so outside that hour AT TIME ZONE reads a wall time unambiguously and the exact
-- function is needed only inside it. Inlined into the calling query.
CREATE FUNCTION control.obehy_instant_fast(service_date date, seconds integer)
RETURNS timestamptz
LANGUAGE sql
STABLE
PARALLEL SAFE
RETURN CASE
    WHEN (service_date + make_interval(secs => seconds))::time >= time '02:00'
        AND (service_date + make_interval(secs => seconds))::time < time '03:00'
    THEN control.obehy_instant(service_date, seconds)
    ELSE (service_date + make_interval(secs => seconds)) AT TIME ZONE 'Europe/Prague'
END;

CREATE INDEX location_name_fold ON static.location
    USING gin (control.obehy_fold(name) gin_trgm_ops);
CREATE INDEX location_parent ON static.location USING btree (load_id, parent_location_id);
CREATE INDEX assignment_trip ON static.assignment USING btree (load_id, trip_id);
CREATE INDEX assignment_route ON static.assignment USING btree (load_id, route_id);
CREATE INDEX assignment_service ON static.assignment USING btree (load_id, service_id);
ANALYZE static.location, static.assignment;

-- The feed trips each current journey publishes over its calls: a rail run's parts (the run's
-- calls first_call..last_call, 0-based), or a road trip as its own part. Rewritten with
-- rt.trip_state_current every emit tick, so the API finds a trip's state by its feed trip ID.
CREATE UNLOGGED TABLE rt.trip_part_current (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    trip_id text NOT NULL,
    first_call integer NOT NULL,
    last_call integer NOT NULL,
    release_id text NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date, trip_id)
);
CREATE INDEX trip_part_trip ON rt.trip_part_current (trip_id, service_date);

CREATE INDEX journey_schedule_trip ON history.journey_schedule (trip_id, service_date);
