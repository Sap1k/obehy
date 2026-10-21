-- Lazy details and compositions (BASE_PLAN.md sections 18.2 and 28, realtime/compositions.py).
-- The API never calls upstream: it records what it was asked (rt.lookup_request) and NOTIFYs
-- obehy_lookup; the worker's lookup broker plans each request through the lookup connectors,
-- fetches under their rates and budgets, and writes the answers.

-- What the API was asked: a trip's composition from a call ('' : the next passenger call). The
-- broker answers it with one outcome per lookup that covers the trip's feed; asked again after
-- its answer, it is pending again.
CREATE TABLE rt.lookup_request (
    kind text NOT NULL,
    feed text NOT NULL,
    trip_id text NOT NULL,
    service_date date NOT NULL,
    from_location text NOT NULL,
    requested_at timestamptz NOT NULL,
    pending boolean NOT NULL,
    answered_at timestamptz,
    -- {"<source>/<lookup>": {outcome: answered | not_found | not_available | failed, reason,
    --   cache_key}}
    outcomes jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (kind, feed, trip_id, service_date, from_location)
);
CREATE INDEX lookup_request_pending ON rt.lookup_request (requested_at) WHERE pending;

-- Every upstream request a lookup sent, with its answer's place in the raw archive: the
-- recorded pairs. Also the daily budget's count.
CREATE TABLE rt.lookup_fetch (
    fetch_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source text NOT NULL,
    lookup text NOT NULL,
    cache_key text NOT NULL,
    requested_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    status integer,
    error text,
    raw_sha256 text,
    outcome text NOT NULL            -- answered | not_found | failed
);
CREATE INDEX lookup_fetch_recent ON rt.lookup_fetch (source, lookup, requested_at);

-- The last answer per upstream question: reused until it expires, by any request planned to
-- the same cache key.
CREATE TABLE rt.detail_cache (
    source text NOT NULL,
    lookup text NOT NULL,
    cache_key text NOT NULL,
    fetch_id bigint NOT NULL,
    outcome text NOT NULL,
    fetched_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    composition_id bigint,           -- an answered composition
    PRIMARY KEY (source, lookup, cache_key)
);

-- A source's answer about a journey's formation, holding from a call (location, visit) until a
-- later answer from a later call closes it (to_* null: open). An identical later answer
-- extends it (observed_at) instead. Kept like history: compositions are a record.
CREATE TABLE rt.composition (
    composition_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feed text NOT NULL,
    key_namespace text NOT NULL,
    key text NOT NULL,
    service_date date NOT NULL,
    source text NOT NULL,
    from_location text NOT NULL,
    from_visit integer NOT NULL,
    to_location text,
    to_visit integer,
    digest text NOT NULL,            -- of the units, to tell identical answers
    first_seen timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    fetch_id bigint NOT NULL,
    UNIQUE (feed, key_namespace, key, service_date, source, from_location, from_visit)
);

-- Its units in train order. A unit's own span (from_*, to_*) is set where it joins or leaves
-- en route; null is the composition's.
CREATE TABLE rt.composition_unit (
    composition_id bigint NOT NULL REFERENCES rt.composition ON DELETE CASCADE,
    sequence integer NOT NULL,
    kind text NOT NULL,              -- locomotive | coach | unit
    role text NOT NULL,              -- train | attached | pusher | raw:<code>
    vehicle_id text,
    vehicle_type text,
    coach_number text,
    classes integer[] NOT NULL,
    features text[] NOT NULL,        -- vocabulary attributes or raw:<source>:<code>
    texts jsonb NOT NULL,            -- {code: the source's own text}
    destination text,
    details jsonb NOT NULL,
    from_location text,
    from_visit integer,
    to_location text,
    to_visit integer,
    PRIMARY KEY (composition_id, sequence)
);

-- A journey's composition units as spans over a trip's calls (the API's composition answer):
-- a unit's span is its own, else its composition's, as the trip's call sequences; an open end
-- is the trip's last call. A unit whose span is not on the trip (an older timetable version) is
-- left out.
CREATE FUNCTION rt.composition_spans(
    for_trip text, for_feed text, for_namespace text, for_key text, for_date date
)
RETURNS TABLE (
    composition_id bigint, source text, observed_at timestamptz, unit_sequence integer,
    kind text, role text, vehicle_id text, vehicle_type text, coach_number text,
    classes integer[], features text[], texts jsonb, destination text, details jsonb,
    from_sequence integer, to_sequence integer, open boolean
)
LANGUAGE sql STABLE AS $$
    WITH calls AS (
        SELECT c.sequence, c.location_id,
               row_number() OVER (PARTITION BY c.location_id ORDER BY c.sequence)::integer
                   AS visit_n
        FROM active.trip_call c
        WHERE c.trip_id = for_trip
    )
    SELECT m.composition_id, m.source, m.observed_at, u.sequence, u.kind, u.role, u.vehicle_id,
           u.vehicle_type, u.coach_number, u.classes, u.features, u.texts, u.destination,
           u.details, f.sequence, coalesce(t.sequence, (SELECT max(sequence) FROM calls)),
           coalesce(u.to_location, m.to_location) IS NULL
    FROM rt.composition m
    JOIN rt.composition_unit u ON u.composition_id = m.composition_id
    JOIN calls f
        ON f.location_id = coalesce(u.from_location, m.from_location)
        AND f.visit_n = coalesce(u.from_visit, m.from_visit)
    LEFT JOIN calls t
        ON t.location_id = coalesce(u.to_location, m.to_location)
        AND t.visit_n = coalesce(u.to_visit, m.to_visit)
    WHERE m.feed = for_feed AND m.key_namespace = for_namespace AND m.key = for_key
        AND m.service_date = for_date
        AND (coalesce(u.to_location, m.to_location) IS NULL OR t.sequence IS NOT NULL)
$$;
