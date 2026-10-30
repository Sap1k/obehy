-- Station pairing (BASE_PLAN.md section 7.4): a railway station shares its search result and
-- board with the surface stop in front of it. Decided once per release at activation
-- (release/pairing.py, policy data/stops/station-pairing-v1.toml), one row per station of the
-- published load pair, ties quarantined; corrected by curated overrides. Presentation only:
-- no ID is merged. `obehy ref import station-pairs` pairs the release already active.

-- The curated overrides (`obehy ref import station-pairs`): `pair` forces a station's stop,
-- `apart` keeps a stop from it, or with an empty stop_id the station from any.
CREATE TABLE ref.station_pair (
    dataset text NOT NULL REFERENCES ref.dataset ON DELETE CASCADE,
    station_id text NOT NULL,
    stop_id text NOT NULL DEFAULT '',
    decision text NOT NULL CHECK (decision IN ('pair', 'apart')),
    note text,
    PRIMARY KEY (dataset, station_id, stop_id)
);

-- One pairing per load pair, with what decided it.
CREATE TABLE control.station_pairing_run (
    jdf_load_id integer NOT NULL REFERENCES control.load (load_id),
    czptt_load_id integer NOT NULL REFERENCES control.load (load_id),
    policy_version text NOT NULL,
    overrides_version text NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (jdf_load_id, czptt_load_id)
);

-- Each served station's outcome: `paired` with a stop (reason `named`, `nearest` or
-- `override`), `ambiguous` (`two_named`, `tie`), `none` (`nothing_near`) or `apart`
-- (`override_apart`). `rule_stop_id` is what the rule alone chose, `candidates` what it saw.
CREATE TABLE control.station_pairing (
    jdf_load_id integer NOT NULL,
    czptt_load_id integer NOT NULL,
    station_id text NOT NULL,
    stop_id text,
    outcome text NOT NULL CHECK (outcome IN ('paired', 'ambiguous', 'none', 'apart')),
    reason text NOT NULL,
    rule_stop_id text,
    candidates jsonb NOT NULL,
    PRIMARY KEY (jdf_load_id, czptt_load_id, station_id),
    FOREIGN KEY (jdf_load_id, czptt_load_id)
        REFERENCES control.station_pairing_run ON DELETE CASCADE,
    CHECK ((outcome = 'paired') = (stop_id IS NOT NULL))
);
CREATE INDEX station_pairing_stop ON control.station_pairing (jdf_load_id, czptt_load_id, stop_id);

-- The published groups: every member with its group (the stop's ID), stations first.
CREATE VIEW active.stop_group AS
    SELECT p.stop_id AS group_id, p.station_id AS location_id, true AS station
    FROM control.station_pairing p
    JOIN control.publication c USING (jdf_load_id, czptt_load_id)
    WHERE p.outcome = 'paired'
    UNION
    SELECT p.stop_id, p.stop_id, false
    FROM control.station_pairing p
    JOIN control.publication c USING (jdf_load_id, czptt_load_id)
    WHERE p.outcome = 'paired';
