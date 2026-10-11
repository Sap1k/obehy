-- Serving 5.2 (contract.MINOR_FIELDS): trip_call.source_kilometre, the source's kilometre
-- value of a call (the JDF kilometre, tariff distance). Null for CZPTT and for packages of an
-- earlier minor. active.trip_call gains the column at the next activation.

ALTER TABLE static.trip_call ADD COLUMN source_kilometre double precision;
