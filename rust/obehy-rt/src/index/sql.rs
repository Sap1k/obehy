//! `Rows` from PostgreSQL `static.*` of one load.
//!
//! Queries read `static.*` by `load_id` rather than `active.*`, so replay can use a retained
//! release that is not the active one; the live worker passes the active publication's loads.
//! The SQL is `index_sql.py`'s, text for text (placeholders aside). Each question is one round
//! trip on a statement prepared once per connection; results arrive in the binary format.
//!
//! The index loader is synchronous (the core stays synchronous); these block on the client's
//! runtime, so they run on a thread outside it (the core's), never on a runtime worker.

use std::collections::HashMap;
use std::error::Error;

use bytes::{BufMut, BytesMut};
use obehy_core::index::{
    CallRow, KeyRow, LocationRow, Rows, RowsError, RunPartRow, ShapePointRow, TripRow,
};
use obehy_core::time::Date;
use tokio::runtime::{Handle, Runtime};
use tokio_postgres::types::{FromSql, IsNull, ToSql, Type, to_sql_checked};
use tokio_postgres::{Client, NoTls, Row, Statement};

/// Days from 1970-01-01 to PostgreSQL's date epoch, 2000-01-01.
const PG_EPOCH_DAYS: i32 = 10_957;

/// A `date` value, in PostgreSQL's binary format (days since 2000-01-01).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PgDate(pub Date);

impl<'a> FromSql<'a> for PgDate {
    fn from_sql(_: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        let days = i32::from_be_bytes(raw.try_into()?);
        Ok(PgDate(Date::from_days(days + PG_EPOCH_DAYS)))
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::DATE
    }
}

impl ToSql for PgDate {
    fn to_sql(&self, _: &Type, out: &mut BytesMut) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        out.put_i32(self.0.days_since_epoch() - PG_EPOCH_DAYS);
        Ok(IsNull::No)
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::DATE
    }

    to_sql_checked!();
}

const KEYS: &str = "SELECT k.namespace, k.identifier, k.public_id, k.entity_kind, k.valid_from, \
 k.valid_to, t.service_id FROM static.source_key k \
 JOIN unnest($1::text[], $2::text[]) AS w (namespace, identifier) \
 USING (namespace, identifier) \
 LEFT JOIN static.trip t ON k.entity_kind = 'trip' \
 AND t.load_id = k.load_id AND t.trip_id = k.public_id \
 WHERE k.load_id = $3 \
 ORDER BY 1, 2, 3, 5";

const LINE_KEYS: &str = "SELECT DISTINCT split_part(identifier, ':', 1), identifier \
 FROM static.source_key WHERE namespace = $1 \
 AND split_part(identifier, ':', 1) = ANY($2) AND load_id = $3";

const TRIPS: &str = "SELECT t.trip_id, t.route_id, coalesce(r.short_name, r.route_id), r.mode, \
 t.service_id, t.headsign, t.shape_id, t.run_key, t.run_part, r.agency_id, \
 (SELECT min(k.identifier) FROM static.source_key k \
  WHERE k.load_id = t.load_id AND k.public_id = t.trip_id \
  AND k.namespace = 'czptt:train_number') \
 FROM static.trip t JOIN static.route r \
 ON r.load_id = t.load_id AND r.route_id = t.route_id \
 WHERE t.trip_id = ANY($1) AND t.load_id = $2";

const CALLS: &str = "SELECT c.trip_id, c.sequence, c.location_id, \
 row_number() OVER (PARTITION BY c.trip_id, c.location_id ORDER BY c.sequence), \
 c.passenger_service, c.scheduled_arrival, c.scheduled_departure, \
 c.pickup_type = 3 OR c.dropoff_type = 3, \
 CASE WHEN p.coordinate_precision = 'exact' THEN p.location_id END \
 FROM static.trip_call c LEFT JOIN static.location p \
 ON p.load_id = c.load_id AND p.location_id = c.boarding_point_id \
 WHERE c.trip_id = ANY($1) AND c.load_id = $2 \
 ORDER BY c.trip_id, c.sequence";

const RUN_PARTS: &str = "SELECT run_key, trip_id, service_id FROM static.trip \
 WHERE run_key = ANY($1) AND load_id = $2 ORDER BY run_key, run_part, trip_id";

const STATION_KEYS: &str = "SELECT identifier, public_id FROM static.source_key \
 WHERE entity_kind = 'location' AND namespace = 'sr70' \
 AND public_id = ANY($1) AND load_id = $2 ORDER BY 1, 2";

const TRACK_KEYS: &str = "SELECT identifier, public_id FROM static.source_key \
 WHERE entity_kind = 'location' AND namespace = 'sr70:track' \
 AND split_part(identifier, ':', 1) = ANY($1) AND load_id = $2 ORDER BY 1, 2";

const LOCATIONS: &str = "SELECT location_id, name, longitude, latitude FROM static.location \
 WHERE location_id = ANY($1) AND load_id = $2";

// The vertices of each shape's line, in order: the loader keeps a shape's geometry only as
// `shape_line` (its `shape_point` rows are emptied once the line is built).
const SHAPE_POINTS: &str = "SELECT s.shape_id, ST_X(d.geom), ST_Y(d.geom) \
 FROM static.shape_line s CROSS JOIN LATERAL ST_DumpPoints(s.geom) d \
 WHERE s.shape_id = ANY($1) AND s.load_id = $2 ORDER BY s.shape_id, d.path[1]";

const SERVICE_DATES: &str = "SELECT service_id, service_date FROM static.service_date \
 WHERE service_id = ANY($1) AND service_date = ANY($2) AND load_id = $3";

/// What drives the client's futures: a runtime of its own (tools, tests), or the worker's
/// (multi-threaded) runtime, entered from the core's thread.
#[derive(Debug)]
pub enum Driver {
    Own(Runtime),
    Shared(Handle),
}

impl Driver {
    pub fn block_on<F: Future>(&self, future: F) -> F::Output {
        match self {
            Driver::Own(runtime) => runtime.block_on(future),
            Driver::Shared(handle) => handle.block_on(future),
        }
    }
}

/// `Rows` from `static.*` of one load.
pub struct SqlRows {
    client: Client,
    driver: Driver,
    load_id: i32,
    statements: HashMap<&'static str, Statement>,
    /// Queries run and the seconds spent waiting for them.
    queries: (usize, f64),
}

impl std::fmt::Debug for SqlRows {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SqlRows")
            .field("load_id", &self.load_id)
            .finish_non_exhaustive()
    }
}

impl SqlRows {
    /// Rows over a connected client whose connection task runs on `driver`.
    pub fn new(client: Client, driver: Driver, load_id: i32) -> SqlRows {
        SqlRows {
            client,
            driver,
            load_id,
            statements: HashMap::new(),
            queries: (0, 0.0),
        }
    }

    /// Connect to `url` on a runtime of its own (a tool's or a test's).
    pub fn connect(url: &str, load_id: i32) -> Result<SqlRows, RowsError> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let client = runtime.block_on(async {
            let (client, connection) = tokio_postgres::connect(url, NoTls).await?;
            tokio::spawn(async move {
                if let Err(error) = connection.await {
                    eprintln!("index database connection: {error}");
                }
            });
            Ok::<_, tokio_postgres::Error>(client)
        })?;
        Ok(SqlRows::new(client, Driver::Own(runtime), load_id))
    }

    /// The client, to run other queries on the same connection (as tools do).
    pub fn client(&self) -> &Client {
        &self.client
    }

    /// Run `future` on the client's runtime.
    pub fn block_on<F: Future>(&self, future: F) -> F::Output {
        self.driver.block_on(future)
    }

    fn query(
        &mut self,
        sql: &'static str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Vec<Row>, RowsError> {
        if !self.statements.contains_key(sql) {
            let statement = self.driver.block_on(self.client.prepare(sql))?;
            self.statements.insert(sql, statement);
        }
        let statement = &self.statements[sql];
        let started = std::time::Instant::now();
        let rows = self.driver.block_on(self.client.query(statement, params))?;
        self.queries.0 += 1;
        self.queries.1 += started.elapsed().as_secs_f64();
        Ok(rows)
    }

    /// The queries run so far and the seconds spent waiting for them.
    pub fn queries(&self) -> (usize, f64) {
        self.queries
    }
}

fn pairs(rows: Vec<Row>) -> Result<Vec<(String, String)>, RowsError> {
    rows.iter()
        .map(|r| Ok((r.try_get(0)?, r.try_get(1)?)))
        .collect()
}

impl Rows for SqlRows {
    fn keys(
        &mut self,
        namespaces: &[&str],
        identifiers: &[&str],
    ) -> Result<Vec<KeyRow>, RowsError> {
        let load_id = self.load_id;
        let rows = self.query(KEYS, &[&namespaces, &identifiers, &load_id])?;
        rows.iter()
            .map(|r| {
                Ok(KeyRow {
                    namespace: r.try_get(0)?,
                    identifier: r.try_get(1)?,
                    public_id: r.try_get(2)?,
                    entity_kind: r.try_get(3)?,
                    valid_from: r.try_get::<_, PgDate>(4)?.0,
                    valid_to: r.try_get::<_, PgDate>(5)?.0,
                    service_id: r.try_get(6)?,
                })
            })
            .collect()
    }

    fn line_keys(
        &mut self,
        namespace: &str,
        lines: &[&str],
    ) -> Result<Vec<(String, String)>, RowsError> {
        let load_id = self.load_id;
        pairs(self.query(LINE_KEYS, &[&namespace, &lines, &load_id])?)
    }

    fn trips(&mut self, trip_ids: &[&str]) -> Result<Vec<TripRow>, RowsError> {
        let load_id = self.load_id;
        let rows = self.query(TRIPS, &[&trip_ids, &load_id])?;
        rows.iter()
            .map(|r| {
                Ok(TripRow {
                    trip_id: r.try_get(0)?,
                    route_id: r.try_get(1)?,
                    route_name: r.try_get(2)?,
                    mode: r.try_get(3)?,
                    service_id: r.try_get(4)?,
                    headsign: r.try_get(5)?,
                    shape_id: r.try_get(6)?,
                    run_key: r.try_get(7)?,
                    run_part: r.try_get(8)?,
                    agency_id: r.try_get(9)?,
                    train_number: r.try_get(10)?,
                })
            })
            .collect()
    }

    fn calls(&mut self, trip_ids: &[&str]) -> Result<Vec<CallRow>, RowsError> {
        let load_id = self.load_id;
        let rows = self.query(CALLS, &[&trip_ids, &load_id])?;
        rows.iter()
            .map(|r| {
                Ok(CallRow {
                    trip_id: r.try_get(0)?,
                    sequence: r.try_get(1)?,
                    location_id: r.try_get(2)?,
                    visit_n: r.try_get(3)?,
                    passenger_service: r.try_get(4)?,
                    arrival: r.try_get(5)?,
                    departure: r.try_get(6)?,
                    on_request: r.try_get(7)?,
                    point_id: r.try_get(8)?,
                })
            })
            .collect()
    }

    fn run_parts(&mut self, run_keys: &[&str]) -> Result<Vec<RunPartRow>, RowsError> {
        let load_id = self.load_id;
        let rows = self.query(RUN_PARTS, &[&run_keys, &load_id])?;
        rows.iter()
            .map(|r| {
                Ok(RunPartRow {
                    run_key: r.try_get(0)?,
                    trip_id: r.try_get(1)?,
                    service_id: r.try_get(2)?,
                })
            })
            .collect()
    }

    fn station_keys(&mut self, location_ids: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        let load_id = self.load_id;
        pairs(self.query(STATION_KEYS, &[&location_ids, &load_id])?)
    }

    fn track_keys(&mut self, codes: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        let load_id = self.load_id;
        pairs(self.query(TRACK_KEYS, &[&codes, &load_id])?)
    }

    fn locations(&mut self, location_ids: &[&str]) -> Result<Vec<LocationRow>, RowsError> {
        let load_id = self.load_id;
        let rows = self.query(LOCATIONS, &[&location_ids, &load_id])?;
        rows.iter()
            .map(|r| {
                Ok(LocationRow {
                    location_id: r.try_get(0)?,
                    name: r.try_get(1)?,
                    lon: r.try_get(2)?,
                    lat: r.try_get(3)?,
                })
            })
            .collect()
    }

    fn shape_points(&mut self, shape_ids: &[&str]) -> Result<Vec<ShapePointRow>, RowsError> {
        let load_id = self.load_id;
        let rows = self.query(SHAPE_POINTS, &[&shape_ids, &load_id])?;
        rows.iter()
            .map(|r| {
                Ok(ShapePointRow {
                    shape_id: r.try_get(0)?,
                    lon: r.try_get(1)?,
                    lat: r.try_get(2)?,
                })
            })
            .collect()
    }

    fn service_dates(
        &mut self,
        service_ids: &[&str],
        days: &[Date],
    ) -> Result<Vec<(String, Date)>, RowsError> {
        let load_id = self.load_id;
        let days: Vec<PgDate> = days.iter().copied().map(PgDate).collect();
        let rows = self.query(SERVICE_DATES, &[&service_ids, &days, &load_id])?;
        rows.iter()
            .map(|r| Ok((r.try_get(0)?, r.try_get::<_, PgDate>(1)?.0)))
            .collect()
    }
}
