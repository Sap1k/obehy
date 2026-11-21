//! The writer's rows to PostgreSQL.
//!
//! Round trips are what a write costs on a remote server, so statements are prepared once per
//! connection and rows are pipelined: sent together and executed in order, as psycopg's
//! `executemany` does. Sets go by binary COPY: `rt.observation`, the state tables and history
//! calls straight into their tables; stop events, assignments and platform evidence, from
//! [`PgStore::copy_from`] rows on, into a temporary stage table followed by one
//! `INSERT .. SELECT .. ON CONFLICT`, its rows folded first so that one statement does what
//! Python's row-by-row upserts did (two rows of one batch with the same conflict key become the
//! row the second upsert would have left). Statements whose effect depends on the previous
//! row's (cancellations, live attributes) always run row by row.
//!
//! Partitions are created on demand, `rt.observation` per UTC hour and history per service
//! date; history rows get the writer's `derivation_id`.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::pin::pin;

use futures_util::future::try_join_all;
use obehy_core::model::{Derivation, Feed, SnapshotJourney};
use obehy_core::time::{Date, Instant};
use tokio_postgres::binary_copy::BinaryCopyInWriter;
use tokio_postgres::types::{ToSql, Type};
use tokio_postgres::{Client, Config, NoTls, Statement};

use super::cells::{Cell, PgTime, Row};
use super::pg_book::{load_registry, load_vehicles, write_physical, write_vehicle_rows};
use super::storage::{
    HISTORY_TABLES, Terms, create_history_partitions, create_observation_partition,
};
use super::vehicles::{RegistryRows, StoredVehicles, VehicleRows};
use super::{OBSERVATION_COLUMNS, Prepare, StateTable, Store, StoreError, StoreResult};
use crate::index::sql::{Driver, PgDate};

/// Sets of at least this many rows go by COPY (stop events, assignments, platform evidence,
/// history calls); smaller ones row by row, pipelined.
pub const COPY_FROM: usize = 64;

/// The binary types of `rt.observation`'s columns, in `OBSERVATION_COLUMNS` order.
const OBSERVATION_TYPES: [Type; 17] = [
    Type::TIMESTAMPTZ,
    Type::TEXT,
    Type::TEXT,
    Type::TEXT,
    Type::TEXT,
    Type::INT4,
    Type::TIMESTAMPTZ,
    Type::INT4,
    Type::INT4,
    Type::JSONB,
    Type::TEXT,
    Type::TEXT,
    Type::DATE,
    Type::TEXT,
    Type::TEXT,
    Type::TEXT,
    Type::TIMESTAMPTZ,
];

/// The binary types of a state table's columns.
fn state_types(table: StateTable) -> Vec<Type> {
    let ty = |column: &str| match column {
        "service_date" => Type::DATE,
        "last_seen" | "updated_at" | "position_at" => Type::TIMESTAMPTZ,
        "latitude" | "longitude" | "bearing" => Type::FLOAT8,
        "delay_s" | "first_call" | "last_call" => Type::INT4,
        "off_route" | "stale" | "represents" => Type::BOOL,
        "calls" | "sources" => Type::JSONB,
        _ => Type::TEXT,
    };
    table.columns.iter().map(|c| ty(c)).collect()
}

fn idents(columns: &[&str]) -> String {
    columns
        .iter()
        .map(|c| format!("\"{c}\""))
        .collect::<Vec<_>>()
        .join(", ")
}

fn params(row: &Row) -> Vec<&(dyn ToSql + Sync)> {
    row.iter().map(|c| c as &(dyn ToSql + Sync)).collect()
}

/// Rows into a prepared `COPY .. FROM STDIN (FORMAT binary)`.
async fn copy_rows(
    client: &Client,
    statement: &Statement,
    types: &[Type],
    rows: &[Row],
) -> Result<u64, tokio_postgres::Error> {
    let sink = client.copy_in(statement).await?;
    let mut writer = pin!(BinaryCopyInWriter::new(sink, types));
    for row in rows {
        writer.as_mut().write(&params(row)).await?;
    }
    writer.finish().await
}

/// Each row through `statement`, pipelined, in order.
async fn each(
    client: &Client,
    statement: &Statement,
    rows: &[Row],
) -> Result<(), tokio_postgres::Error> {
    let all: Vec<Vec<&(dyn ToSql + Sync)>> = rows.iter().map(params).collect();
    try_join_all(all.iter().map(|p| client.execute(statement, p))).await?;
    Ok(())
}

/// `rows` folded by their first `width` cells: the first row's position, merged with each later
/// one by `merge`.
fn fold_by(rows: &[Row], width: usize, merge: impl Fn(&mut Row, &Row)) -> Vec<Row> {
    let key = |row: &Row| -> String {
        // A text unique per value tuple (the key cells are text, ints and dates).
        row[..width]
            .iter()
            .map(|c| format!("{c:?}"))
            .collect::<Vec<_>>()
            .join("\u{1}")
    };
    let mut at: BTreeMap<String, usize> = BTreeMap::new();
    let mut out: Vec<Row> = Vec::with_capacity(rows.len());
    for row in rows {
        match at.get(&key(row)) {
            Some(&i) => merge(&mut out[i], row),
            None => {
                at.insert(key(row), out.len());
                out.push(row.clone());
            }
        }
    }
    out
}

fn earlier(a: &Cell, b: &Cell) -> Cell {
    match (a, b) {
        (Cell::Time(x), Cell::Time(y)) => Cell::Time((*x).min(*y)),
        _ => a.clone(),
    }
}

fn later(a: &Cell, b: &Cell) -> Cell {
    match (a, b) {
        (Cell::Time(x), Cell::Time(y)) => Cell::Time((*x).max(*y)),
        _ => a.clone(),
    }
}

// --- SQL ---------------------------------------------------------------------------------------

const INSERT_JOURNEY: &str = "INSERT INTO history.journey (feed, key_namespace, key, \
 service_date, first_bound_at, latest_revision) VALUES ($1, $2, $3, $4, $5, 0) \
 ON CONFLICT DO NOTHING";

const SNAPSHOT_SELECT: &str = "SELECT j.journey_id, j.latest_revision, s.release_id, s.trip_id \
 FROM history.journey j \
 LEFT JOIN history.journey_schedule s USING (feed, key_namespace, key, service_date) \
 WHERE (j.feed, j.key_namespace, j.key, j.service_date) = ($1, $2, $3, $4) \
 AND (s.revision IS NULL OR s.revision = j.latest_revision)";

const INSERT_SCHEDULE: &str = "INSERT INTO history.journey_schedule (feed, key_namespace, key, \
 service_date, revision, release_id, trip_id, route_name, headsign, derivation_id, run_key, \
 route_id, mode, agency_id) \
 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)";

const UPDATE_JOURNEY: &str = "UPDATE history.journey SET latest_revision = $1 \
 WHERE (feed, key_namespace, key, service_date) = ($2, $3, $4, $5)";

const CALL_COLUMNS: &str = "journey_id, service_date, revision, ordinal, location_term, visit_n, \
 passenger_service, scheduled_arrival, scheduled_departure, name_term";

const INSERT_CALL: &str = "INSERT INTO history.journey_call (journey_id, service_date, \
 revision, ordinal, location_term, visit_n, passenger_service, scheduled_arrival, \
 scheduled_departure, name_term) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)";

const CALL_TYPES: [Type; 10] = [
    Type::INT8,
    Type::DATE,
    Type::INT2,
    Type::INT2,
    Type::INT4,
    Type::INT2,
    Type::BOOL,
    Type::INT4,
    Type::INT4,
    Type::INT4,
];

const ORPHANS: &str = "UPDATE history.actual_stop_event e SET \
 orphaned = NOT EXISTS (SELECT 1 FROM history.journey_call c \
   WHERE (c.journey_id, c.service_date, c.revision, c.location_term, c.visit_n) \
       = (e.journey_id, e.service_date, $1, e.location_term, e.visit_n)), \
 revision = CASE WHEN EXISTS (SELECT 1 FROM history.journey_call c \
   WHERE (c.journey_id, c.service_date, c.revision, c.location_term, c.visit_n) \
       = (e.journey_id, e.service_date, $1, e.location_term, e.visit_n)) \
   THEN $1 ELSE e.revision END \
 WHERE (e.journey_id, e.service_date) = ($2, $3)";

const EVENT_CONFLICT: &str = "ON CONFLICT (journey_id, service_date, location_term, visit_n, \
 event_type) DO UPDATE SET revision = excluded.revision, event_time = excluded.event_time, \
 interval_lo = excluded.interval_lo, interval_hi = excluded.interval_hi, \
 method_term = excluded.method_term, source_term = excluded.source_term, orphaned = false, \
 derivation_id = excluded.derivation_id";

const EVENT_COLUMNS_SQL: &str = "journey_id, service_date, location_term, visit_n, event_type, \
 revision, event_time, interval_lo, interval_hi, method_term, source_term, derivation_id";

const ASSIGNMENT_CONFLICT: &str = "ON CONFLICT (feed, source, source_vehicle_id, key_namespace, \
 key, service_date) DO UPDATE SET \
 first_seen = least(history.vehicle_assignment.first_seen, excluded.first_seen), \
 last_seen = greatest(history.vehicle_assignment.last_seen, excluded.last_seen), \
 vehicle_id = excluded.vehicle_id";

const ASSIGNMENT_COLUMNS_SQL: &str = "feed, source, source_vehicle_id, key_namespace, key, \
 service_date, first_seen, last_seen, method, derivation_id, vehicle_id";

const PLATFORM_CONFLICT: &str = "ON CONFLICT (feed, key_namespace, key, service_date, \
 location_id, visit_n, event_type, value) DO UPDATE SET \
 first_seen = least(history.platform_evidence.first_seen, excluded.first_seen), \
 last_seen = greatest(history.platform_evidence.last_seen, excluded.last_seen), \
 boarding_point_id = excluded.boarding_point_id, label = excluded.label";

const PLATFORM_COLUMNS_SQL: &str = "feed, key_namespace, key, service_date, location_id, visit_n, \
 event_type, value, label, boarding_point_id, source, first_seen, last_seen, derivation_id";

const LINK: &str = "INSERT INTO history.journey_link (feed, source_namespace, source_key, \
 target_namespace, target_key, service_date, kind, derivation_id) \
 VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING";

const CANCEL: &str = "INSERT INTO history.journey_cancellation (feed, key_namespace, key, \
 service_date, source, first_seen, last_seen, derivation_id) \
 VALUES ($1, $2, $3, $4, $5, $6, $6, $7) \
 ON CONFLICT (feed, key_namespace, key, service_date, source) DO UPDATE SET \
 first_seen = least(history.journey_cancellation.first_seen, excluded.first_seen), \
 last_seen = greatest(history.journey_cancellation.last_seen, excluded.last_seen), \
 restored_at = CASE WHEN history.journey_cancellation.restored_at < excluded.last_seen \
 THEN NULL ELSE history.journey_cancellation.restored_at END";

const RESTORE: &str = "UPDATE history.journey_cancellation SET restored_at = $1 \
 WHERE (feed, key_namespace, key, service_date) = ($2, $3, $4, $5) \
 AND restored_at IS NULL AND last_seen <= $1";

/// A stage table: its name, definition and the COPY columns with their types.
struct Stage {
    name: &'static str,
    definition: &'static str,
    columns: &'static str,
    types: &'static [Type],
}

const EVENT_STAGE: Stage = Stage {
    name: "event_stage",
    definition: "feed text, key_namespace text, key text, service_date date, \
     location_term int, visit_n int, event_type text, event_time timestamptz, \
     interval_lo timestamptz, interval_hi timestamptz, method_term int, source_term int",
    columns: "feed, key_namespace, key, service_date, location_term, visit_n, event_type, \
     event_time, interval_lo, interval_hi, method_term, source_term",
    types: &[
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::DATE,
        Type::INT4,
        Type::INT4,
        Type::TEXT,
        Type::TIMESTAMPTZ,
        Type::TIMESTAMPTZ,
        Type::TIMESTAMPTZ,
        Type::INT4,
        Type::INT4,
    ],
};

const ASSIGNMENT_STAGE: Stage = Stage {
    name: "assignment_stage",
    definition: "feed text, source text, source_vehicle_id text, key_namespace text, key text, \
     service_date date, first_seen timestamptz, last_seen timestamptz, method text, \
     vehicle_id text",
    columns: "feed, source, source_vehicle_id, key_namespace, key, service_date, first_seen, \
     last_seen, method, vehicle_id",
    types: &[
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::DATE,
        Type::TIMESTAMPTZ,
        Type::TIMESTAMPTZ,
        Type::TEXT,
        Type::TEXT,
    ],
};

const PLATFORM_STAGE: Stage = Stage {
    name: "platform_stage",
    definition: "feed text, key_namespace text, key text, service_date date, location_id text, \
     visit_n int, event_type text, value text, label text, boarding_point_id text, \
     source text, first_seen timestamptz, last_seen timestamptz",
    columns: "feed, key_namespace, key, service_date, location_id, visit_n, event_type, value, \
     label, boarding_point_id, source, first_seen, last_seen",
    types: &[
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::DATE,
        Type::TEXT,
        Type::INT4,
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::TEXT,
        Type::TIMESTAMPTZ,
        Type::TIMESTAMPTZ,
    ],
};

/// A new schedule's parameters and its journey's revision update.
type ScheduleWrite<'a> = ([&'a (dyn ToSql + Sync); 14], [&'a (dyn ToSql + Sync); 5]);

/// The writer's rows to PostgreSQL.
pub struct PgStore {
    client: Client,
    driver: Driver,
    pub derivation_id: i32,
    /// Sets of at least this many rows go by COPY ([`COPY_FROM`]).
    pub copy_from: usize,
    terms: Terms,
    hours: HashSet<Instant>,
    days: HashSet<Date>,
    /// `rt.physical_vehicles`' rows, written when the writer opens.
    physical: Vec<(String, String, bool)>,
    statements: HashMap<String, Statement>,
    stages: HashSet<&'static str>,
}

impl std::fmt::Debug for PgStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PgStore")
            .field("derivation_id", &self.derivation_id)
            .finish_non_exhaustive()
    }
}

impl PgStore {
    /// A store over a connected client whose connection runs on `driver`; `physical` is each
    /// source's `[vehicles] physical` (`vehicles::physical_rows`).
    pub fn new(client: Client, driver: Driver, physical: Vec<(String, String, bool)>) -> PgStore {
        PgStore {
            client,
            driver,
            derivation_id: 0,
            copy_from: COPY_FROM,
            terms: Terms::default(),
            hours: HashSet::new(),
            days: HashSet::new(),
            physical,
            statements: HashMap::new(),
            stages: HashSet::new(),
        }
    }

    /// Connect to `url` on a runtime of its own (tools, tests, a thread of the worker).
    pub fn connect(url: &str, physical: Vec<(String, String, bool)>) -> StoreResult<PgStore> {
        let config: Config = url.parse()?;
        PgStore::connect_config(&config, physical)
    }

    /// Connect with `config` on a runtime of its own.
    pub fn connect_config(
        config: &Config,
        physical: Vec<(String, String, bool)>,
    ) -> StoreResult<PgStore> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| StoreError::other(e.to_string()))?;
        let client = runtime.block_on(async {
            let (client, connection) = config.connect(NoTls).await?;
            tokio::spawn(async move {
                if let Err(error) = connection.await {
                    crate::logs::log(&format!("store database connection: {error}"));
                }
            });
            Ok::<_, tokio_postgres::Error>(client)
        })?;
        Ok(PgStore::new(client, Driver::Own(runtime), physical))
    }

    pub fn client(&self) -> &Client {
        &self.client
    }

    /// Run `future` on the client's runtime.
    pub fn block_on<F: Future>(&self, future: F) -> F::Output {
        self.driver.block_on(future)
    }

    /// The statements of `sqls`, prepared once per connection (the missing ones together).
    fn prepared(&mut self, sqls: &[&str]) -> StoreResult<Vec<Statement>> {
        let missing: Vec<&str> = sqls
            .iter()
            .copied()
            .filter(|s| !self.statements.contains_key(*s))
            .collect();
        if !missing.is_empty() {
            let client = &self.client;
            let found = self
                .driver
                .block_on(try_join_all(missing.iter().map(|s| client.prepare(s))))?;
            for (sql, statement) in missing.into_iter().zip(found) {
                self.statements.insert(sql.to_owned(), statement);
            }
        }
        Ok(sqls.iter().map(|s| self.statements[*s].clone()).collect())
    }

    /// A temporary stage table (rows deleted at commit), created once per connection; its COPY.
    fn stage(&mut self, stage: &Stage) -> StoreResult<Statement> {
        if !self.stages.contains(stage.name) {
            self.driver.block_on(self.client.batch_execute(&format!(
                "CREATE TEMP TABLE IF NOT EXISTS {} ({}) ON COMMIT DELETE ROWS",
                stage.name, stage.definition
            )))?;
            self.stages.insert(stage.name);
        }
        let copy = format!(
            "COPY {} ({}) FROM STDIN (FORMAT binary)",
            stage.name, stage.columns
        );
        Ok(self.prepared(&[&copy])?.remove(0))
    }

    /// `rows` into `stage`, then `insert` (which reads it) and the stage emptied, pipelined.
    fn staged(&mut self, stage: &Stage, rows: &[Row], insert: &str) -> StoreResult<()> {
        let copy = self.stage(stage)?;
        let empty = format!("DELETE FROM {}", stage.name);
        let statements = self.prepared(&[insert, &empty])?;
        let client = &self.client;
        let derivation_id = self.derivation_id;
        self.driver.block_on(async {
            copy_rows(client, &copy, stage.types, rows).await?;
            let insert_params: [&(dyn ToSql + Sync); 1] = [&derivation_id];
            tokio::try_join!(
                client.execute(&statements[0], &insert_params),
                client.execute(&statements[1], &[]),
            )?;
            Ok(())
        })
    }

    fn term(&self, cell: &Cell) -> StoreResult<Cell> {
        let text = cell.as_text().unwrap_or_default();
        Ok(Cell::Int(i64::from(self.terms.get(text)?)))
    }
}

impl Store for PgStore {
    fn open(&mut self, d: &Derivation) -> StoreResult<StoredVehicles> {
        let client = &self.client;
        let physical = self.physical.clone();
        let (derivation_id, terms, stored) = self.driver.block_on(async {
            client
                .execute(
                    "INSERT INTO history.derivation (core_version, policy_version, release_id) \
                     VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    &[&d.core_version, &d.policy_version, &d.release_id],
                )
                .await?;
            let row = client
                .query_one(
                    "SELECT derivation_id FROM history.derivation \
                     WHERE core_version = $1 AND policy_version = $2 AND release_id = $3",
                    &[&d.core_version, &d.policy_version, &d.release_id],
                )
                .await?;
            let terms = Terms::load(client).await?;
            let stored = load_vehicles(client).await?;
            write_physical(client, &physical).await?;
            Ok::<_, StoreError>((row.try_get::<_, i32>(0)?, terms, stored))
        })?;
        self.derivation_id = derivation_id;
        self.terms = terms;
        Ok(stored)
    }

    fn reload_registry(&mut self) -> StoreResult<RegistryRows> {
        self.driver.block_on(load_registry(&self.client))
    }

    fn prepare(&mut self, prepare: &Prepare) -> StoreResult<()> {
        let client = &self.client;
        let (hours, days, terms) = (&mut self.hours, &mut self.days, &mut self.terms);
        self.driver.block_on(async {
            for hour in &prepare.hours {
                if !hours.contains(hour) {
                    create_observation_partition(client, *hour).await?;
                    hours.insert(*hour);
                }
            }
            for day in &prepare.days {
                if !days.contains(day) {
                    create_history_partitions(client, *day).await?;
                    days.insert(*day);
                }
            }
            terms.add(client, &prepare.terms).await
        })
    }

    fn begin(&mut self) -> StoreResult<()> {
        Ok(self.driver.block_on(self.client.batch_execute("BEGIN"))?)
    }

    fn commit(&mut self) -> StoreResult<()> {
        Ok(self.driver.block_on(self.client.batch_execute("COMMIT"))?)
    }

    fn rollback(&mut self) {
        let _ = self.driver.block_on(self.client.batch_execute("ROLLBACK"));
    }

    fn observations(&mut self, rows: &[Row]) -> StoreResult<()> {
        if rows.is_empty() {
            return Ok(());
        }
        let copy = format!(
            "COPY rt.observation ({}) FROM STDIN (FORMAT binary)",
            idents(&OBSERVATION_COLUMNS)
        );
        let copy = self.prepared(&[&copy])?.remove(0);
        self.driver
            .block_on(copy_rows(&self.client, &copy, &OBSERVATION_TYPES, rows))?;
        Ok(())
    }

    fn snapshots(&mut self, snapshots: &[&SnapshotJourney]) -> StoreResult<()> {
        if snapshots.is_empty() {
            return Ok(());
        }
        let copy_calls =
            format!("COPY history.journey_call ({CALL_COLUMNS}) FROM STDIN (FORMAT binary)");
        let st = self.prepared(&[
            INSERT_JOURNEY,
            SNAPSHOT_SELECT,
            INSERT_SCHEDULE,
            UPDATE_JOURNEY,
            INSERT_CALL,
            ORPHANS,
            &copy_calls,
        ])?;
        let st = &st;
        let client = &self.client;
        let terms = &self.terms;
        let derivation_id = self.derivation_id;
        let copy_from = self.copy_from;
        self.driver.block_on(async {
            // Snapshots go in waves of distinct journeys, each wave two pipelined round trips
            // (journey and current schedule; new schedule and revision): within a wave no
            // statement reads what another snapshot's writes, and a journey snapshotted again
            // waits for the next wave. Every snapshot's calls go in after the snapshots
            // (nothing between reads them); the orphan updates, which do, after the calls.
            let mut calls: Vec<Row> = Vec::new();
            let mut orphans: Vec<(i16, i64, PgDate)> = Vec::new();
            let mut start = 0;
            while start < snapshots.len() {
                let mut seen = HashSet::new();
                let mut end = start;
                while end < snapshots.len() && seen.insert(&snapshots[end].journey) {
                    end += 1;
                }
                let wave = &snapshots[start..end];
                start = end;
                let keyed: Vec<(&str, PgDate, PgTime)> = wave
                    .iter()
                    .map(|s| {
                        (
                            s.journey.feed.as_str(),
                            PgDate(s.journey.service_date),
                            PgTime(s.at),
                        )
                    })
                    .collect();
                let journey_params: Vec<[&(dyn ToSql + Sync); 5]> = wave
                    .iter()
                    .zip(&keyed)
                    .map(|(s, (feed, day, at))| -> [&(dyn ToSql + Sync); 5] {
                        [feed, &s.journey.namespace, &s.journey.key, day, at]
                    })
                    .collect();
                let found = try_join_all(journey_params.iter().map(|p| async move {
                    let (_, row) = tokio::try_join!(
                        client.execute(&st[0], p),
                        client.query_one(&st[1], &p[..4]),
                    )?;
                    Ok::<_, tokio_postgres::Error>(row)
                }))
                .await?;
                // (snapshot, its keys, journey ID, latest revision, new revision)
                let mut changed = Vec::new();
                for ((snapshot, keys), row) in wave.iter().zip(&keyed).zip(found) {
                    let journey_id: i64 = row.try_get(0)?;
                    let latest: i32 = row.try_get(1)?;
                    let release_id: Option<String> = row.try_get(2)?;
                    let trip_id: Option<String> = row.try_get(3)?;
                    if release_id.as_deref() == Some(snapshot.release_id.as_str())
                        && trip_id.as_deref() == Some(snapshot.trip_id.as_str())
                    {
                        continue;
                    }
                    changed.push((*snapshot, keys, journey_id, latest, latest + 1));
                }
                let writes: Vec<ScheduleWrite> = changed
                    .iter()
                    .map(|(snapshot, (feed, day, _), _, _, revision)| {
                        let j = &snapshot.journey;
                        (
                            [
                                feed as &(dyn ToSql + Sync),
                                &j.namespace,
                                &j.key,
                                day,
                                revision,
                                &snapshot.release_id,
                                &snapshot.trip_id,
                                &snapshot.route_name,
                                &snapshot.headsign,
                                &derivation_id,
                                &snapshot.run_key,
                                &snapshot.route_id,
                                &snapshot.mode,
                                &snapshot.agency_id,
                            ],
                            [
                                revision as &(dyn ToSql + Sync),
                                feed,
                                &j.namespace,
                                &j.key,
                                day,
                            ],
                        )
                    })
                    .collect();
                try_join_all(writes.iter().map(|(schedule, update)| async move {
                    tokio::try_join!(
                        client.execute(&st[2], schedule),
                        client.execute(&st[3], update),
                    )
                }))
                .await?;
                for (snapshot, (_, day, _), journey_id, latest, revision) in &changed {
                    for c in snapshot.calls.iter() {
                        calls.push(vec![
                            Cell::Int(*journey_id),
                            Cell::Date(snapshot.journey.service_date),
                            Cell::Int(i64::from(*revision)),
                            Cell::Int(c.ordinal),
                            Cell::Int(i64::from(terms.get(&c.location_id)?)),
                            Cell::Int(i64::from(c.visit_n)),
                            Cell::Bool(c.passenger_service),
                            Cell::opt_int(c.scheduled_arrival.map(i64::from)),
                            Cell::opt_int(c.scheduled_departure.map(i64::from)),
                            Cell::Int(i64::from(terms.get(&c.name)?)),
                        ]);
                    }
                    if *latest > 0 {
                        // Re-attach events by (location_id, visit_n); a vanished call orphans
                        // its events.
                        let revision = i16::try_from(*revision)
                            .map_err(|e| StoreError::other(e.to_string()))?;
                        orphans.push((revision, *journey_id, *day));
                    }
                }
            }
            if calls.len() >= copy_from {
                copy_rows(client, &st[6], &CALL_TYPES, &calls).await?;
            } else {
                each(client, &st[4], &calls).await?;
            }
            let orphan_params: Vec<[&(dyn ToSql + Sync); 3]> = orphans
                .iter()
                .map(|(revision, id, day)| -> [&(dyn ToSql + Sync); 3] { [revision, id, day] })
                .collect();
            try_join_all(orphan_params.iter().map(|p| client.execute(&st[5], p))).await?;
            Ok(())
        })
    }

    fn events(&mut self, rows: &[Row]) -> StoreResult<()> {
        if rows.is_empty() {
            return Ok(());
        }
        let mut staged = Vec::with_capacity(rows.len());
        for row in rows {
            let mut row = row.clone();
            for i in [4, 10, 11] {
                row[i] = self.term(&row[i])?;
            }
            staged.push(row);
        }
        if rows.len() >= self.copy_from {
            // Later rows of one conflict key replace every updated column: the last one stands.
            let folded = fold_by(&staged, 7, |kept, row| kept.clone_from(row));
            let insert = format!(
                "INSERT INTO history.actual_stop_event ({EVENT_COLUMNS_SQL}) \
                 SELECT j.journey_id, j.service_date, s.location_term, s.visit_n, \
                 s.event_type::history.event_kind, j.latest_revision, s.event_time, \
                 s.interval_lo, s.interval_hi, s.method_term, s.source_term, $1::integer \
                 FROM event_stage s JOIN history.journey j \
                 ON (j.feed, j.key_namespace, j.key, j.service_date) \
                  = (s.feed, s.key_namespace, s.key, s.service_date) {EVENT_CONFLICT}"
            );
            return self.staged(&EVENT_STAGE, &folded, &insert);
        }
        let insert = format!(
            "INSERT INTO history.actual_stop_event ({EVENT_COLUMNS_SQL}) \
             SELECT j.journey_id, j.service_date, $1::integer, $2::smallint, \
             $3::text::history.event_kind, j.latest_revision, $4::timestamptz, $5::timestamptz, \
             $6::timestamptz, $7::integer, $8::integer, $9::integer \
             FROM history.journey j \
             WHERE (j.feed, j.key_namespace, j.key, j.service_date) = ($10, $11, $12, $13) \
             {EVENT_CONFLICT}"
        );
        let statement = self.prepared(&[&insert])?.remove(0);
        let derivation = Cell::Int(i64::from(self.derivation_id));
        let ordered: Vec<Row> = staged
            .into_iter()
            .map(|r| {
                let mut out: Row = r[4..].to_vec();
                out.insert(8, derivation.clone());
                out.extend_from_slice(&r[..4]);
                out
            })
            .collect();
        self.driver
            .block_on(each(&self.client, &statement, &ordered))?;
        Ok(())
    }

    fn assignments(&mut self, rows: &[Row]) -> StoreResult<()> {
        if rows.is_empty() {
            return Ok(());
        }
        if rows.len() >= self.copy_from {
            // first_seen least, last_seen greatest, vehicle_id the last's; method stays the
            // first's.
            let folded = fold_by(rows, 6, |kept, row| {
                kept[6] = earlier(&kept[6], &row[6]);
                kept[7] = later(&kept[7], &row[7]);
                kept[9] = row[9].clone();
            });
            let insert = format!(
                "INSERT INTO history.vehicle_assignment ({ASSIGNMENT_COLUMNS_SQL}) \
                 SELECT feed, source, source_vehicle_id, key_namespace, key, service_date, \
                 first_seen, last_seen, method, $1::integer, vehicle_id FROM assignment_stage \
                 {ASSIGNMENT_CONFLICT}"
            );
            return self.staged(&ASSIGNMENT_STAGE, &folded, &insert);
        }
        let insert = format!(
            "INSERT INTO history.vehicle_assignment ({ASSIGNMENT_COLUMNS_SQL}) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) {ASSIGNMENT_CONFLICT}"
        );
        let statement = self.prepared(&[&insert])?.remove(0);
        let derivation = Cell::Int(i64::from(self.derivation_id));
        let ordered: Vec<Row> = rows
            .iter()
            .map(|r| {
                let mut out = r.clone();
                out.insert(9, derivation.clone());
                out
            })
            .collect();
        self.driver
            .block_on(each(&self.client, &statement, &ordered))?;
        Ok(())
    }

    fn links(&mut self, rows: &[Row]) -> StoreResult<()> {
        if rows.is_empty() {
            return Ok(());
        }
        let statement = self.prepared(&[LINK])?.remove(0);
        let derivation = Cell::Int(i64::from(self.derivation_id));
        let ordered: Vec<Row> = rows
            .iter()
            .map(|r| {
                let mut out = r.clone();
                out.push(derivation.clone());
                out
            })
            .collect();
        self.driver
            .block_on(each(&self.client, &statement, &ordered))?;
        Ok(())
    }

    fn platforms(&mut self, rows: &[Row]) -> StoreResult<()> {
        if rows.is_empty() {
            return Ok(());
        }
        if rows.len() >= self.copy_from {
            // first_seen least, last_seen greatest, boarding point and label the last's.
            let folded = fold_by(rows, 8, |kept, row| {
                kept[11] = earlier(&kept[11], &row[11]);
                kept[12] = later(&kept[12], &row[12]);
                kept[9] = row[9].clone();
                kept[8] = row[8].clone();
            });
            let insert = format!(
                "INSERT INTO history.platform_evidence ({PLATFORM_COLUMNS_SQL}) \
                 SELECT {}, $1::integer FROM platform_stage {PLATFORM_CONFLICT}",
                PLATFORM_STAGE.columns
            );
            return self.staged(&PLATFORM_STAGE, &folded, &insert);
        }
        let insert = format!(
            "INSERT INTO history.platform_evidence ({PLATFORM_COLUMNS_SQL}) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) \
             {PLATFORM_CONFLICT}"
        );
        let statement = self.prepared(&[&insert])?.remove(0);
        let derivation = Cell::Int(i64::from(self.derivation_id));
        let ordered: Vec<Row> = rows
            .iter()
            .map(|r| {
                let mut out = r.clone();
                out.push(derivation.clone());
                out
            })
            .collect();
        self.driver
            .block_on(each(&self.client, &statement, &ordered))?;
        Ok(())
    }

    /// A cancellation is kept per journey and source (first and last said); a vehicle seen
    /// running the journey after all restores it, whichever source cancelled it, until a later
    /// cancellation.
    fn cancellations(&mut self, rows: &[Row]) -> StoreResult<()> {
        if rows.is_empty() {
            return Ok(());
        }
        let st = self.prepared(&[CANCEL, RESTORE])?;
        let derivation = Cell::Int(i64::from(self.derivation_id));
        let client = &self.client;
        self.driver.block_on(async {
            let values: Vec<(bool, Vec<&(dyn ToSql + Sync)>)> = rows
                .iter()
                .map(|row| {
                    let cancelled = matches!(row[6], Cell::Bool(true));
                    let p: Vec<&(dyn ToSql + Sync)> = if cancelled {
                        vec![
                            &row[0],
                            &row[1],
                            &row[2],
                            &row[3],
                            &row[4],
                            &row[5],
                            &derivation,
                        ]
                    } else {
                        vec![&row[5], &row[0], &row[1], &row[2], &row[3]]
                    };
                    (cancelled, p)
                })
                .collect();
            try_join_all(
                values.iter().map(|(cancelled, p)| {
                    client.execute(if *cancelled { &st[0] } else { &st[1] }, p)
                }),
            )
            .await?;
            Ok(())
        })
    }

    fn vehicles(&mut self, rows: &VehicleRows) -> StoreResult<()> {
        self.driver.block_on(write_vehicle_rows(&self.client, rows))
    }

    fn replace_state(&mut self, table: StateTable, feed: Feed, rows: &[Row]) -> StoreResult<()> {
        let delete = format!("DELETE FROM rt.\"{}\" WHERE feed = $1", table.name);
        let copy = format!(
            "COPY rt.\"{}\" ({}) FROM STDIN (FORMAT binary)",
            table.name,
            idents(table.columns)
        );
        let st = self.prepared(&[&delete, &copy])?;
        let client = &self.client;
        self.driver.block_on(async {
            client.execute(&st[0], &[&feed.as_str()]).await?;
            if !rows.is_empty() {
                copy_rows(client, &st[1], &state_types(table), rows).await?;
            }
            Ok(())
        })
    }

    fn delete_state(&mut self, table: StateTable, keys: &[Row]) -> StoreResult<()> {
        if keys.is_empty() {
            return Ok(());
        }
        let columns: Vec<Vec<Option<String>>> = (0..table.key.len())
            .map(|i| {
                keys.iter()
                    .map(|k| match &k[i] {
                        Cell::Text(t) => Some(t.clone()),
                        Cell::Date(d) => Some(d.isoformat()),
                        Cell::Null => None,
                        other => Some(format!("{other:?}")),
                    })
                    .collect()
            })
            .collect();
        let arrays: Vec<String> = table
            .key_types
            .iter()
            .enumerate()
            .map(|(i, t)| format!("${}::text[]::{t}[]", i + 1))
            .collect();
        let delete = format!(
            "DELETE FROM rt.\"{}\" WHERE ({}) IN (SELECT * FROM unnest({}))",
            table.name,
            idents(table.key),
            arrays.join(", ")
        );
        let statement = self.prepared(&[&delete])?.remove(0);
        let values: Vec<&(dyn ToSql + Sync)> =
            columns.iter().map(|c| c as &(dyn ToSql + Sync)).collect();
        self.driver
            .block_on(self.client.execute(&statement, &values))?;
        Ok(())
    }

    fn upsert_state(&mut self, table: StateTable, rows: &[Row]) -> StoreResult<()> {
        if rows.is_empty() {
            return Ok(());
        }
        let stage = format!("{}_stage", table.name);
        let columns = idents(table.columns);
        let updates: Vec<&str> = table
            .columns
            .iter()
            .copied()
            .filter(|c| !table.key.contains(c))
            .collect();
        let excluded: Vec<String> = updates
            .iter()
            .map(|c| format!("EXCLUDED.\"{c}\""))
            .collect();
        if !self.stages.contains(table.name) {
            self.driver.block_on(self.client.batch_execute(&format!(
                "CREATE TEMP TABLE IF NOT EXISTS \"{stage}\" (LIKE rt.\"{}\") \
                 ON COMMIT DELETE ROWS",
                table.name
            )))?;
            self.stages.insert(table.name);
        }
        let copy = format!("COPY \"{stage}\" ({columns}) FROM STDIN (FORMAT binary)");
        let insert = format!(
            "INSERT INTO rt.\"{}\" ({columns}) SELECT {columns} FROM \"{stage}\" \
             ON CONFLICT ({}) DO UPDATE SET ({}) = ({})",
            table.name,
            idents(table.key),
            idents(&updates),
            excluded.join(", ")
        );
        let empty = format!("DELETE FROM \"{stage}\"");
        let st = self.prepared(&[&copy, &insert, &empty])?;
        let client = &self.client;
        self.driver.block_on(async {
            copy_rows(client, &st[0], &state_types(table), rows).await?;
            tokio::try_join!(client.execute(&st[1], &[]), client.execute(&st[2], &[]))?;
            Ok(())
        })
    }

    fn clear_history(&mut self, days: &[Date]) -> StoreResult<()> {
        if days.is_empty() {
            return Ok(());
        }
        let days: Vec<PgDate> = days.iter().copied().map(PgDate).collect();
        let client = &self.client;
        self.driver.block_on(async {
            client.batch_execute("BEGIN").await?;
            let outcome = async {
                for table in HISTORY_TABLES {
                    client
                        .execute(
                            &format!(
                                "DELETE FROM history.\"{table}\" WHERE service_date = ANY($1)"
                            ),
                            &[&days],
                        )
                        .await?;
                }
                Ok::<_, tokio_postgres::Error>(())
            }
            .await;
            match outcome {
                Ok(()) => Ok(client.batch_execute("COMMIT").await?),
                Err(error) => {
                    let _ = client.batch_execute("ROLLBACK").await;
                    Err(error.into())
                }
            }
        })
    }
}

/// Delete stored observations of `sources` received in `[start, end)` before a replay.
pub async fn clear_observations(
    client: &Client,
    start: Instant,
    end: Instant,
    sources: &[&str],
) -> StoreResult<u64> {
    Ok(client
        .execute(
            "DELETE FROM rt.observation WHERE received_at >= $1 AND received_at < $2 \
             AND source = ANY($3)",
            &[&PgTime(start), &PgTime(end), &sources],
        )
        .await?)
}
