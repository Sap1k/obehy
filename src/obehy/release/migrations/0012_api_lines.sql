-- The project API's second set of lookups (BASE_PLAN.md section 30): a stop's features, a
-- trip's connections, line search regardless of case and diacritics, and history by journey
-- key without its feed (links, schedules). Loads build the static indexes on each partition
-- before ATTACH (ddl.INDEXES); here they reach the partitions already loaded.

CREATE INDEX assignment_location ON static.assignment USING btree (load_id, location_id);
CREATE INDEX connection_origin ON static.connection_claim USING btree (load_id, origin_trip_id);
CREATE INDEX route_name_fold ON static.route
    USING gin (control.obehy_fold(coalesce(short_name, '') || ' ' || coalesce(long_name, ''))
        gin_trgm_ops);
ANALYZE static.assignment, static.connection_claim, static.route;

CREATE INDEX journey_link_source ON history.journey_link
    (source_namespace, source_key, service_date);
CREATE INDEX journey_link_target ON history.journey_link
    (target_namespace, target_key, service_date);
CREATE INDEX journey_schedule_key ON history.journey_schedule
    (key_namespace, key, service_date);
