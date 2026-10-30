-- One service date's vehicle days: the chains history.vehicle_chains cuts (migration 0025),
-- which the API reads live by the same rule.
INSERT INTO history.vehicle_day (feed, source, source_vehicle_id, service_date, seq, journeys,
    first_seen, last_seen)
SELECT feed, source, source_vehicle_id, service_date, seq, journeys, first_seen, last_seen
FROM history.vehicle_chains(%(day)s::date, %(gap_s)s::integer)
