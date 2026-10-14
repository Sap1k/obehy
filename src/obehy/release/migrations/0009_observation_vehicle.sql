-- Warm replay by vehicle (docs/R1_SLICE.md section 5): each stored observation names its vehicle
-- and the reception time from which the vehicle's state depends on its observations, so a
-- restart replays the vehicles still live from there instead of the whole window. Rows of
-- earlier versions have neither: warm replay reads them from the window start.

ALTER TABLE rt.observation ADD COLUMN vehicle_id text, ADD COLUMN state_since timestamptz;
