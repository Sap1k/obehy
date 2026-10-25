-- The source time of a vehicle's position, apart from `last_seen` (its last report): a report
-- without a position, an older fix or the same place again (the SŽ map's document clock moves on
-- unchanged entries) leaves it. Readers choose and order positions by it. NULL until written.

ALTER TABLE rt.vehicle_state_current ADD COLUMN position_at timestamptz;
