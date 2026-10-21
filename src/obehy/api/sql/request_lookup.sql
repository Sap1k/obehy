-- Ask the lookup broker (realtime/runtime/lookups.py) for a detail; asked again once answered,
-- the request is pending again. The caller NOTIFYs obehy_lookup.
INSERT INTO rt.lookup_request AS r
    (kind, feed, trip_id, service_date, from_location, requested_at, pending, outcomes)
VALUES (%(kind)s, %(feed)s, %(trip_id)s, %(service_date)s, %(from_location)s,
        statement_timestamp(), true, '{}')
ON CONFLICT (kind, feed, trip_id, service_date, from_location) DO UPDATE
SET requested_at = excluded.requested_at, pending = true, outcomes = '{}'
WHERE NOT r.pending
