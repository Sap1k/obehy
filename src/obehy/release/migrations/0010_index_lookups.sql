-- Indexes for the realtime index loader's lookups (realtime/index_sql.py), which otherwise
-- scan a whole partition or namespace on every load: a rail run's parts, the train number of a
-- trip and the sr70 keys of locations (by public ID), and a line's trip keys and a station's
-- tracks (by the identifier's prefix). Loads build them on each partition before ATTACH
-- (ddl.INDEXES); here they reach the partitions already loaded.

CREATE INDEX trip_run ON static.trip USING btree (load_id, run_key);
CREATE INDEX source_key_public ON static.source_key USING btree (load_id, namespace, public_id);
CREATE INDEX source_key_prefix ON static.source_key
    USING btree (load_id, namespace, split_part(identifier, ':', 1));
ANALYZE static.trip, static.source_key;
