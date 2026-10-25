-- A report on a rail run is the train its number names (emit/vehicles.py): `train:<number>`,
-- bound by 'train'.

ALTER TABLE rt.vehicle_state_current DROP CONSTRAINT vehicle_state_current_bound_by_check;
ALTER TABLE rt.vehicle_state_current ADD CONSTRAINT vehicle_state_current_bound_by_check
    CHECK (bound_by IN ('source', 'identifier', 'co_located', 'train'));
