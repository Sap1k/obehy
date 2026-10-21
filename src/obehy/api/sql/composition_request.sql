-- What the API was last asked about this trip's composition from this call, and the answer.
SELECT requested_at, pending, answered_at, outcomes
FROM rt.lookup_request
WHERE kind = 'composition' AND feed = %(feed)s AND trip_id = %(trip_id)s
    AND service_date = %(service_date)s AND from_location = %(from_location)s
