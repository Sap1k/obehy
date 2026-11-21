//! The Rust store against Python's (R7): sends the row records of an output stream
//! (`obehy rt compare --level output`) through [`PgStore`] into the database named by the
//! environment variable `--url-env`, write by write, as `rust/tools/store_db.py apply` sends
//! them through Python's `SqlStore`. `rust/tools/store_db.py parity` runs both and compares the
//! tables.
//!
//! ```text
//! cargo run --release -p obehy-rt --example store_parity -- --stream S --url-env VAR //!     [--limit N] [--copy-from N] [--only observation]
//! ```

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant as Clock;

use obehy_core::config::manifest;
use obehy_core::model::Derivation;
use obehy_core::model::{Feed, SnapshotJourney};
use obehy_core::time::Date;
use obehy_rt::settings::Paths;
use obehy_rt::store::vehicles::physical_rows;
use obehy_rt::store::vehicles::{RegistryRows, StoredVehicles, VehicleRows};
use obehy_rt::store::{PgStore, Prepare, Row, StateTable, Store, StoreResult, records};

/// A store that times each of its methods (seconds and calls, by name).
struct Timed<S> {
    store: S,
    spent: std::collections::BTreeMap<&'static str, (f64, usize)>,
}

impl<S: Store> Timed<S> {
    fn time<T>(&mut self, name: &'static str, f: impl FnOnce(&mut S) -> T) -> T {
        let started = Clock::now();
        let out = f(&mut self.store);
        let slot = self.spent.entry(name).or_default();
        slot.0 += started.elapsed().as_secs_f64();
        slot.1 += 1;
        out
    }
}

impl<S: Store> Store for Timed<S> {
    fn open(&mut self, d: &Derivation) -> StoreResult<StoredVehicles> {
        self.time("open", |s| s.open(d))
    }
    fn reload_registry(&mut self) -> StoreResult<RegistryRows> {
        self.time("reload_registry", |s| s.reload_registry())
    }
    fn prepare(&mut self, p: &Prepare) -> StoreResult<()> {
        self.time("prepare", |s| s.prepare(p))
    }
    fn begin(&mut self) -> StoreResult<()> {
        self.time("begin", |s| s.begin())
    }
    fn commit(&mut self) -> StoreResult<()> {
        self.time("commit", |s| s.commit())
    }
    fn rollback(&mut self) {
        self.time("rollback", |s| s.rollback());
    }
    fn observations(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.time("observations", |s| s.observations(rows))
    }
    fn snapshots(&mut self, snapshots: &[&SnapshotJourney]) -> StoreResult<()> {
        self.time("snapshots", |s| s.snapshots(snapshots))
    }
    fn events(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.time("events", |s| s.events(rows))
    }
    fn assignments(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.time("assignments", |s| s.assignments(rows))
    }
    fn links(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.time("links", |s| s.links(rows))
    }
    fn platforms(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.time("platforms", |s| s.platforms(rows))
    }
    fn cancellations(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.time("cancellations", |s| s.cancellations(rows))
    }
    fn vehicles(&mut self, rows: &VehicleRows) -> StoreResult<()> {
        self.time("vehicles", |s| s.vehicles(rows))
    }
    fn replace_state(&mut self, t: StateTable, feed: Feed, rows: &[Row]) -> StoreResult<()> {
        self.time("replace_state", |s| s.replace_state(t, feed, rows))
    }
    fn delete_state(&mut self, t: StateTable, keys: &[Row]) -> StoreResult<()> {
        self.time("delete_state", |s| s.delete_state(t, keys))
    }
    fn upsert_state(&mut self, t: StateTable, rows: &[Row]) -> StoreResult<()> {
        self.time("upsert_state", |s| s.upsert_state(t, rows))
    }
    fn clear_history(&mut self, days: &[Date]) -> StoreResult<()> {
        self.time("clear_history", |s| s.clear_history(days))
    }
}

fn open(path: &PathBuf) -> std::io::Result<Box<dyn BufRead>> {
    let file = File::open(path)?;
    Ok(if path.extension().is_some_and(|e| e == "gz") {
        Box::new(BufReader::new(flate2::read::MultiGzDecoder::new(file)))
    } else {
        Box::new(BufReader::new(file))
    })
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut stream = None;
    let mut url_env = None;
    let mut limit = None;
    let mut copy_from = None;
    let mut observations_only = false;
    let mut items = std::env::args().skip(1);
    while let Some(flag) = items.next() {
        let value = items.next().ok_or(format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--stream" => stream = Some(PathBuf::from(value)),
            "--url-env" => url_env = Some(value),
            "--limit" => limit = Some(value.parse()?),
            "--copy-from" => copy_from = Some(value.parse()?),
            "--only" if value == "observation" => observations_only = true,
            _ => return Err(format!("unknown flag {flag}").into()),
        }
    }
    let stream = stream.ok_or("--stream is required")?;
    let url = std::env::var(url_env.ok_or("--url-env is required")?)?;
    let header = records::header(open(&stream)?)?;
    let text = |key: &str| header[key].as_str().unwrap_or_default().to_owned();
    let derivation = Derivation {
        core_version: text("core_version"),
        policy_version: text("policy_version"),
        release_id: text("release"),
    };
    let paths = Paths::discover()?;
    let sources = manifest::load_sources(&paths.sources(), &manifest::RECORDER_FILTERS)?;
    let mut pg = PgStore::connect(&url, physical_rows(&sources))?;
    if let Some(copy_from) = copy_from {
        pg.copy_from = copy_from;
    }
    let mut store = Timed {
        store: pg,
        spent: Default::default(),
    };
    store.open(&derivation)?;
    let started = Clock::now();
    let mut count = 0;
    let mut writes = 0;
    for group in records::groups(open(&stream)?, limit) {
        let mut group = group?;
        if observations_only {
            group.retain(|r| r["kind"] == "observation");
        }
        count += group.len();
        writes += 1;
        records::apply(&mut store, &group)?;
    }
    println!(
        "{count} records in {} writes, read and written in {:.1} s",
        writes,
        started.elapsed().as_secs_f64()
    );
    let spent: Vec<String> = store
        .spent
        .iter()
        .map(|(name, (s, n))| format!("{name} {s:.2} s/{n}"))
        .collect();
    println!("by method: {}", spent.join(", "));
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("store_parity: {error}");
            ExitCode::FAILURE
        }
    }
}
