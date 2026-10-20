-- The connections a trip's timetable promises (waits for, connects to), with the target trip
-- when the claim was resolved to one running on the date.
SELECT c.direction, c.origin_sequence AS at_sequence, c.wait_minutes,
       c.target_public_line AS line, c.target_destination_text AS destination, c.note,
       t.trip_id AS target_trip_id
FROM active.connection_claim c
LEFT JOIN active.trip t ON t.trip_id = c.target_trip_id
    AND EXISTS (
        SELECT 1 FROM active.service_date d
        WHERE d.service_id = t.service_id AND d.service_date = %(service_date)s
    )
WHERE c.origin_trip_id = %(trip_id)s
ORDER BY c.origin_sequence NULLS FIRST, c.direction, c.connection_id
