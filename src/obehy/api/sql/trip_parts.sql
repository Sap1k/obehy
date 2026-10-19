-- The parts of a rail run running on the date, in order.
SELECT t.trip_id, t.run_part
FROM active.trip t
WHERE t.run_key = %(run_key)s
    AND EXISTS (
        SELECT 1 FROM active.service_date d
        WHERE d.service_id = t.service_id AND d.service_date = %(service_date)s
    )
ORDER BY t.run_part, t.trip_id
