-- Cancelled journeys (BASE_PLAN.md section 20.7): per public journey and the source cancelling
-- it, when the source first and last said the journey does not run, and when a vehicle was seen
-- running it after all. Partitioned like the rest of history; the writer creates partitions.
CREATE TABLE history.journey_cancellation (
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    source text NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    restored_at timestamptz,
    derivation_id integer NOT NULL,
    PRIMARY KEY (feed, key_namespace, key, service_date, source)
) PARTITION BY RANGE (service_date);
