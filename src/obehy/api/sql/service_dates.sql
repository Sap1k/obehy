-- The dates services run between two service dates.
SELECT service_id, array_agg(service_date ORDER BY service_date) AS dates
FROM active.service_date
WHERE service_id = ANY(%(service_ids)s::text[]) AND service_date BETWEEN %(since)s AND %(until)s
GROUP BY service_id
ORDER BY service_id
