-- One row per release directory (obehy build run) that was registered for loading.
CREATE TABLE control.release (
    run_id text PRIMARY KEY,
    completed_at timestamptz,
    gvd_year integer,
    release_json jsonb NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT now()
);

-- One row per production package of a release.
CREATE TABLE control.package (
    run_id text NOT NULL REFERENCES control.release (run_id),
    package text NOT NULL CHECK (package IN ('jdf', 'czptt')),
    feed_version text NOT NULL,
    manifest_sha256 text NOT NULL,
    package_sha256 text NOT NULL,
    serving_schema_version text NOT NULL,
    manifest jsonb NOT NULL,
    PRIMARY KEY (run_id, package)
);

-- One row per load attempt; load_id is the partition key of every static.* table.
CREATE TABLE control.load (
    load_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL,
    package text NOT NULL,
    status text NOT NULL CHECK (status IN ('loading', 'loaded', 'failed', 'dropped')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    dropped_at timestamptz,
    row_counts jsonb,
    warnings jsonb,
    error text,
    FOREIGN KEY (run_id, package) REFERENCES control.package (run_id, package)
);
CREATE INDEX load_package ON control.load (run_id, package, status);

-- Activations and rollbacks. previous_seq is where a rollback from this entry returns.
CREATE TABLE control.publication_history (
    seq integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL REFERENCES control.release (run_id),
    jdf_load_id integer NOT NULL REFERENCES control.load (load_id),
    czptt_load_id integer NOT NULL REFERENCES control.load (load_id),
    action text NOT NULL CHECK (action IN ('activate', 'rollback')),
    previous_seq integer REFERENCES control.publication_history (seq),
    activated_at timestamptz NOT NULL DEFAULT now()
);

-- The active release: a single row, null until the first activation.
CREATE TABLE control.publication (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    history_seq integer REFERENCES control.publication_history (seq),
    run_id text REFERENCES control.release (run_id),
    jdf_load_id integer REFERENCES control.load (load_id),
    czptt_load_id integer REFERENCES control.load (load_id),
    activated_at timestamptz
);
INSERT INTO control.publication DEFAULT VALUES;
