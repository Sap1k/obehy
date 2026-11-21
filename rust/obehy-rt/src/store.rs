//! Where the worker's rows go: port of `emit/rows.py`, `emit/db.py`, `emit/sql_store.py`,
//! `emit/storage.py`, `emit/stored.py`, `emit/recording.py` and the writing halves of
//! `emit/compositions.py`, `emit/alerts.py` and `emit/vehicles.py` (docs/RUST_PORT.md, R7).
//!
//! [`Writer`] turns the core's effects and state into rows; a [`Store`] writes them:
//! [`PgStore`] to PostgreSQL (binary COPY where Python sends many rows), [`RecordingStore`]
//! as the compare harness's records. Rows are [`Row`]s, a cell per column in the order of the
//! `*_COLUMNS` constants, as Python's tuples are. History terms (`history.term`) and the
//! writer's `derivation_id` are the SQL store's to add: a row names a term by its text and
//! carries no derivation.
//!
//! The store is synchronous, as the core and the runner are: [`PgStore`] blocks on the client's
//! runtime from the runner's thread (as the index's `SqlRows` does).

pub mod alerts;
pub mod cells;
pub mod compositions;
pub mod pg;
pub mod pg_book;
pub mod recording;
pub mod records;
pub mod rows;
pub mod storage;
pub mod stored;
pub mod vehicles;
pub mod writer;

use std::collections::BTreeSet;

use obehy_core::model::{Derivation, Feed, SnapshotJourney};
use obehy_core::time::{Date, Instant};

pub use cells::{Cell, Row};
pub use pg::PgStore;
pub use recording::RecordingStore;
pub use vehicles::{StoredVehicles, VehicleRows};
pub use writer::Writer;

/// A store failed; the write's transaction is rolled back.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("database: {}", db_message(.0))]
    Database(#[from] tokio_postgres::Error),
    #[error("{0}")]
    Other(String),
}

impl StoreError {
    pub fn other(message: impl Into<String>) -> StoreError {
        StoreError::Other(message.into())
    }
}

pub type StoreResult<T> = Result<T, StoreError>;

/// A database error with the server's message and code, when it has one.
fn db_message(error: &tokio_postgres::Error) -> String {
    match error.as_db_error() {
        Some(db) => format!("{} ({})", db.message(), db.code().code()),
        None => error.to_string(),
    }
}

// --- columns ---------------------------------------------------------------------------------

/// `rt.observation`; `facts` is the fact JSON (`model_json.facts_to_json`).
pub const OBSERVATION_COLUMNS: [&str; 17] = [
    "received_at",
    "source",
    "channel",
    "feed",
    "raw_sha256",
    "raw_item",
    "observed_at",
    "decoder_version",
    "fact_schema_version",
    "facts",
    "journey_namespace",
    "journey_key",
    "service_date",
    "reason",
    "release_id",
    "vehicle_id",
    "state_since",
];
/// `location`, `method` and `source` are terms; `event_time` is the interval's middle.
pub const EVENT_COLUMNS: [&str; 12] = [
    "feed",
    "key_namespace",
    "key",
    "service_date",
    "location",
    "visit_n",
    "event_type",
    "event_time",
    "interval_lo",
    "interval_hi",
    "method",
    "source",
];
pub const ASSIGNMENT_COLUMNS: [&str; 10] = [
    "feed",
    "source",
    "source_vehicle_id",
    "key_namespace",
    "key",
    "service_date",
    "first_seen",
    "last_seen",
    "method",
    "vehicle_id",
];
pub const LINK_COLUMNS: [&str; 7] = [
    "feed",
    "source_namespace",
    "source_key",
    "target_namespace",
    "target_key",
    "service_date",
    "kind",
];
pub const PLATFORM_COLUMNS: [&str; 13] = [
    "feed",
    "key_namespace",
    "key",
    "service_date",
    "location_id",
    "visit_n",
    "event_type",
    "value",
    "label",
    "boarding_point_id",
    "source",
    "first_seen",
    "last_seen",
];
/// `cancelled` false: seen running after all (restores the journey).
pub const CANCELLATION_COLUMNS: [&str; 7] = [
    "feed",
    "key_namespace",
    "key",
    "service_date",
    "source",
    "at",
    "cancelled",
];

/// A current-state table of `rt`: its columns, key and the key's SQL types.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StateTable {
    pub name: &'static str,
    pub columns: &'static [&'static str],
    pub key: &'static [&'static str],
    pub key_types: &'static [&'static str],
}

impl StateTable {
    /// The key cells of a row (the key columns lead every table's columns).
    pub fn key_of(&self, row: &Row) -> Row {
        row[..self.key.len()].to_vec()
    }
}

pub const VEHICLES: StateTable = StateTable {
    name: "vehicle_state_current",
    columns: &[
        "feed",
        "source",
        "source_vehicle_id",
        "status",
        "last_seen",
        "key_namespace",
        "key",
        "service_date",
        "trip_id",
        "latitude",
        "longitude",
        "bearing",
        "reason",
        "release_id",
        "vehicle_id",
        "bound_by",
        "represents",
        "position_at",
    ],
    key: &["feed", "source", "source_vehicle_id"],
    key_types: &["text", "text", "text"],
};

/// `calls` and `sources` are JSON (jsonb columns).
pub const TRIPS: StateTable = StateTable {
    name: "trip_state_current",
    columns: &[
        "feed",
        "key_namespace",
        "key",
        "service_date",
        "release_id",
        "trip_id",
        "lifecycle",
        "delay_s",
        "off_route",
        "stale",
        "updated_at",
        "calls",
        "sources",
    ],
    key: &["feed", "key_namespace", "key", "service_date"],
    key_types: &["text", "text", "text", "date"],
};

/// The feed trips each journey publishes over its calls (BASE_PLAN.md section 30).
pub const PARTS: StateTable = StateTable {
    name: "trip_part_current",
    columns: &[
        "feed",
        "key_namespace",
        "key",
        "service_date",
        "trip_id",
        "first_call",
        "last_call",
        "release_id",
    ],
    key: &["feed", "key_namespace", "key", "service_date", "trip_id"],
    key_types: &["text", "text", "text", "date", "text"],
};

pub const STATE_TABLES: [StateTable; 3] = [VEHICLES, TRIPS, PARTS];

/// What a write is about to need before its transaction: observation hours (UTC), history
/// service dates and terms.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Prepare {
    pub hours: BTreeSet<Instant>,
    pub days: BTreeSet<Date>,
    pub terms: Vec<String>,
}

/// Where the writer's rows go (`emit/rows.py`'s `Store`). Each `Writer::write` and
/// `write_state` is one transaction (`begin` .. `commit`, or `rollback` after an error);
/// `prepare` runs before it, outside.
pub trait Store {
    /// Start writing under `derivation`; what the database holds of the vehicle book.
    fn open(&mut self, derivation: &Derivation) -> StoreResult<StoredVehicles>;

    /// The vehicle registry again (`obehy ref import` loaded a dataset).
    fn reload_registry(&mut self) -> StoreResult<vehicles::RegistryRows>;

    fn prepare(&mut self, prepare: &Prepare) -> StoreResult<()>;

    fn begin(&mut self) -> StoreResult<()>;
    fn commit(&mut self) -> StoreResult<()>;
    fn rollback(&mut self);

    fn observations(&mut self, rows: &[Row]) -> StoreResult<()>;

    /// Journeys' schedules, in order: each a new revision unless the stored one has its release
    /// and trip.
    fn snapshots(&mut self, snapshots: &[&SnapshotJourney]) -> StoreResult<()>;

    fn events(&mut self, rows: &[Row]) -> StoreResult<()>;
    fn assignments(&mut self, rows: &[Row]) -> StoreResult<()>;
    fn links(&mut self, rows: &[Row]) -> StoreResult<()>;
    fn platforms(&mut self, rows: &[Row]) -> StoreResult<()>;
    fn cancellations(&mut self, rows: &[Row]) -> StoreResult<()>;

    /// Live attributes, pairings and profiles (the vehicle book's rows).
    fn vehicles(&mut self, rows: &VehicleRows) -> StoreResult<()>;

    /// A feed's rows of `table`, replacing all it had.
    fn replace_state(&mut self, table: StateTable, feed: Feed, rows: &[Row]) -> StoreResult<()>;
    fn delete_state(&mut self, table: StateTable, keys: &[Row]) -> StoreResult<()>;
    fn upsert_state(&mut self, table: StateTable, rows: &[Row]) -> StoreResult<()>;

    /// Delete the history of service dates before a rebuild by replay.
    fn clear_history(&mut self, days: &[Date]) -> StoreResult<()>;
}
