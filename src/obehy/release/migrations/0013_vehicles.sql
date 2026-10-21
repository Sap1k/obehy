-- The vehicle registry (BASE_PLAN.md section 27). Curated datasets are mirrored into ref.* by
-- `obehy ref import`: Oběhy's own vehicle list over seeds such as the DÚK register, a vehicle
-- known by keys (a source's vehicle ID, a licence plate) valid over dates. The worker resolves
-- which vehicle each source report is and what is known about it (reference/registry.py,
-- reference/precedence.py) and writes the result; the API only reads it.
CREATE SCHEMA ref;

-- One imported version of a dataset; its rows are replaced as a whole on import. The lower
-- `priority` wins where datasets disagree.
CREATE TABLE ref.dataset (
    dataset text PRIMARY KEY,
    version text NOT NULL,
    title text NOT NULL,
    private boolean NOT NULL,
    priority integer NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    rows integer NOT NULL
);

CREATE TABLE ref.vehicle_model (
    vehicle_model_id text NOT NULL,
    dataset text NOT NULL REFERENCES ref.dataset ON DELETE CASCADE,
    manufacturer text,
    model text NOT NULL,
    mode text,
    PRIMARY KEY (vehicle_model_id, dataset)
);

-- Several datasets may describe one vehicle (same vehicle_id): their rows combine.
CREATE TABLE ref.vehicle (
    vehicle_id text NOT NULL,
    dataset text NOT NULL REFERENCES ref.dataset ON DELETE CASCADE,
    vehicle_model_id text,
    public_label text,
    PRIMARY KEY (vehicle_id, dataset)
);

-- How a vehicle is recognised: by a source's vehicle ID, or by an identifier whichever source
-- states it (`scheme`: licence_plate, evn, series_number; `identifier` normalised as the vehicle
-- vocabulary says).
CREATE TABLE ref.vehicle_key (
    dataset text NOT NULL REFERENCES ref.dataset ON DELETE CASCADE,
    vehicle_id text NOT NULL,
    source text,
    source_vehicle_id text,
    scheme text,
    identifier text,
    valid_from date NOT NULL,
    valid_to date,
    CHECK ((source IS NOT NULL AND source_vehicle_id IS NOT NULL)
        <> (scheme IS NOT NULL AND identifier IS NOT NULL))
);

CREATE TABLE ref.vehicle_attribute (
    vehicle_id text NOT NULL,
    attribute text NOT NULL,
    value text NOT NULL,
    dataset text NOT NULL REFERENCES ref.dataset ON DELETE CASCADE,
    PRIMARY KEY (vehicle_id, attribute, dataset)
);

CREATE TABLE ref.model_attribute (
    vehicle_model_id text NOT NULL,
    attribute text NOT NULL,
    value text NOT NULL,
    dataset text NOT NULL REFERENCES ref.dataset ON DELETE CASCADE,
    PRIMARY KEY (vehicle_model_id, attribute, dataset)
);

-- The current value each source states about its vehicles, written on change (and touched
-- every attributes.toml touch_s). Kept across restarts: warm replay does not rebuild it.
CREATE TABLE rt.vehicle_attribute_observed (
    source text NOT NULL,
    source_vehicle_id text NOT NULL,
    attribute text NOT NULL,
    value text NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    PRIMARY KEY (source, source_vehicle_id, attribute)
);

-- What is known about each vehicle, as the worker resolves it: the effective attributes (the
-- registry > live by source precedence > the model's default), every statement behind them,
-- and the sources stating them. Derived, rebuilt by the worker at start: UNLOGGED.
CREATE UNLOGGED TABLE rt.vehicle_profile (
    vehicle_id text PRIMARY KEY,
    registered boolean NOT NULL,
    public_label text,
    manufacturer text,
    model text,
    low_floor boolean,
    air_conditioning boolean,
    wheelchair_accessible boolean,
    operator text,
    licence_plate text,
    fleet_number text,
    datasets text[] NOT NULL,
    sources text[] NOT NULL,
    attributes jsonb NOT NULL,
    last_stated timestamptz -- the latest live statement
);

-- Which vehicle each source report is (the registry's when a key binds it, else its own
-- source form), and by which key.
ALTER TABLE rt.vehicle_state_current
    ADD COLUMN vehicle_id text NOT NULL DEFAULT '',
    ADD COLUMN bound_by text CHECK (bound_by IN ('source', 'identifier', 'co_located'));
CREATE INDEX vehicle_state_vehicle ON rt.vehicle_state_current (vehicle_id);

-- Two sources' reports seen running one journey side by side (emit/pairing.py): while
-- confirmed they are one vehicle; each pairing is a candidate key for the registry.
CREATE TABLE rt.vehicle_pairing (
    source_a text NOT NULL,
    source_vehicle_a text NOT NULL,
    source_b text NOT NULL,
    source_vehicle_b text NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen timestamptz NOT NULL,
    fixes integer NOT NULL,
    confirmed boolean NOT NULL,
    journey jsonb NOT NULL,  -- the last journey they ran together
    PRIMARY KEY (source_a, source_vehicle_a, source_b, source_vehicle_b)
);

-- Which sources a journey's realtime came from: per source, when it last brought anything new,
-- when it last gave a usable position, its delay, and whether it leads the position.
ALTER TABLE rt.trip_state_current ADD COLUMN sources jsonb NOT NULL DEFAULT '[]'::jsonb;
