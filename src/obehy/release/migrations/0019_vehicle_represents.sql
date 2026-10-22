-- Which vehicle stands for its journey (emit/represent.py, the GTFS-RT rule): the SŽ map's
-- train and the unit's GPS from PID or DÚK are one train, a bus reported by DÚK and a city's
-- feed is one bus. The map lists only these; a trip lists them first. The worker writes it on
-- every row; true until it does.

ALTER TABLE rt.vehicle_state_current ADD COLUMN represents boolean NOT NULL DEFAULT true;
