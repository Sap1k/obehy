//! `obehy-rt bench --from-files`: the live worker's per-poll and per-tick path over archived
//! polls (port of `realtime/bench.py` without the database): decode, core, the writer's rows
//! (made and dropped by a [`NullStore`]), and the emit tick (estimates, state tables, GTFS-RT
//! files) on a simulated clock, with the index read from the release's serving files.
//!
//! - CPU and wall time per phase, also as a share of one core over the simulated time and per
//!   observation; observations a second over the whole run (decoding runs on a pool ahead of
//!   the core: its CPU is summed over the pool's threads);
//! - every `--sample-every` simulated seconds (default an hour): the resident memory and the
//!   size of every long-lived container (index, plans, state, the writer's rows, the vehicle
//!   book, the GTFS-RT emitters); the peak resident memory at the end.
//!
//! It is also the memory soak (docs/RUST_PORT.md "Memory"): several days of archive at full
//! speed, `--switch-release` naming the releases activated on the way. The payloads are
//! archived after the recorder's filters, and the bench does not archive them again (Python's
//! `archive` phase).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant as Monotonic};

use obehy_core::config::Policy;
use obehy_core::core::CORE_VERSION;
use obehy_core::index::{
    CallRow, KeyRow, LocationRow, Rows, RowsError, RunPartRow, ShapePointRow, TripRow,
};
use obehy_core::model::{Derivation, Effect, Feed, FeedState, SnapshotJourney};
use obehy_core::publish::gtfs_rt::FeedEmitter;
use obehy_core::runner::{Loads, Runner, Ticks};
use obehy_core::time::{Date, Instant, TimeDelta};
use obehy_core::vehicles::VehicleBook;
use serde_json::{Value, json};

use crate::index::{ServingRows, release_run_id, serving_dir};
use crate::output::harness_book;
use crate::phases::Phases;
use crate::replay::{DecodeTimes, ReplayError, decoded_polls};
use crate::store::vehicles::{Book, RegistryRows, StoredVehicles, VehicleRows};
use crate::store::{Prepare, Row, StateTable, Store, StoreResult, Writer};
use crate::warm::rss_bytes;

/// A [`Store`] that drops every row it is sent: the writer still makes them (CPU measured,
/// nothing sent), and the vehicle book starts as on an empty database.
#[derive(Debug, Default)]
pub struct NullStore;

impl Store for NullStore {
    fn open(&mut self, _derivation: &Derivation) -> StoreResult<StoredVehicles> {
        Ok(StoredVehicles::default())
    }
    fn reload_registry(&mut self) -> StoreResult<RegistryRows> {
        Ok(RegistryRows::default())
    }
    fn prepare(&mut self, _prepare: &Prepare) -> StoreResult<()> {
        Ok(())
    }
    fn begin(&mut self) -> StoreResult<()> {
        Ok(())
    }
    fn commit(&mut self) -> StoreResult<()> {
        Ok(())
    }
    fn rollback(&mut self) {}
    fn observations(&mut self, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn snapshots(&mut self, _snapshots: &[&SnapshotJourney]) -> StoreResult<()> {
        Ok(())
    }
    fn events(&mut self, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn assignments(&mut self, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn links(&mut self, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn platforms(&mut self, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn cancellations(&mut self, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn vehicles(&mut self, _rows: &VehicleRows) -> StoreResult<()> {
        Ok(())
    }
    fn replace_state(&mut self, _table: StateTable, _feed: Feed, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn delete_state(&mut self, _table: StateTable, _keys: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn upsert_state(&mut self, _table: StateTable, _rows: &[Row]) -> StoreResult<()> {
        Ok(())
    }
    fn clear_history(&mut self, _days: &[Date]) -> StoreResult<()> {
        Ok(())
    }
}

// --- the index's rows, shared ----------------------------------------------------------------

/// A release's serving tables, each feed's read whole once and shared by every runtime made
/// over them (compactions make fresh runtimes): the files stand in for PostgreSQL by holding
/// the release in memory, so that memory is a constant measured apart (`tables_rss_mb`), not
/// something a compaction doubles.
#[derive(Debug, Clone)]
pub struct SharedLoads {
    pub run_id: String,
    tables: BTreeMap<Feed, Arc<Mutex<ServingRows>>>,
}

impl SharedLoads {
    pub fn open(release: &Path, feeds: &[Feed]) -> Result<SharedLoads, RowsError> {
        let mut tables = BTreeMap::new();
        for &feed in feeds {
            let mut rows = ServingRows::open(&serving_dir(release, feed))?;
            rows.read_all()?;
            tables.insert(feed, Arc::new(Mutex::new(rows)));
        }
        Ok(SharedLoads {
            run_id: release_run_id(release)?,
            tables,
        })
    }
}

impl Loads for SharedLoads {
    type Rows = SharedRows;

    fn release_id(&self) -> &str {
        &self.run_id
    }

    fn rows(&self, feed: Feed) -> Result<SharedRows, RowsError> {
        let table = self
            .tables
            .get(&feed)
            .ok_or_else(|| format!("no serving tables of {}", feed.as_str()))?;
        Ok(SharedRows(Arc::clone(table)))
    }
}

/// One feed's shared serving tables as `Rows`.
#[derive(Debug, Clone)]
pub struct SharedRows(Arc<Mutex<ServingRows>>);

impl SharedRows {
    fn with<T>(&self, f: impl FnOnce(&mut ServingRows) -> T) -> T {
        f(&mut self.0.lock().expect("the serving tables"))
    }
}

impl Rows for SharedRows {
    fn keys(
        &mut self,
        namespaces: &[&str],
        identifiers: &[&str],
    ) -> Result<Vec<KeyRow>, RowsError> {
        self.with(|r| r.keys(namespaces, identifiers))
    }
    fn line_keys(
        &mut self,
        namespace: &str,
        lines: &[&str],
    ) -> Result<Vec<(String, String)>, RowsError> {
        self.with(|r| r.line_keys(namespace, lines))
    }
    fn trips(&mut self, trip_ids: &[&str]) -> Result<Vec<TripRow>, RowsError> {
        self.with(|r| r.trips(trip_ids))
    }
    fn calls(&mut self, trip_ids: &[&str]) -> Result<Vec<CallRow>, RowsError> {
        self.with(|r| r.calls(trip_ids))
    }
    fn run_parts(&mut self, run_keys: &[&str]) -> Result<Vec<RunPartRow>, RowsError> {
        self.with(|r| r.run_parts(run_keys))
    }
    fn station_keys(&mut self, location_ids: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        self.with(|r| r.station_keys(location_ids))
    }
    fn track_keys(&mut self, codes: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        self.with(|r| r.track_keys(codes))
    }
    fn locations(&mut self, location_ids: &[&str]) -> Result<Vec<LocationRow>, RowsError> {
        self.with(|r| r.locations(location_ids))
    }
    fn shape_points(&mut self, shape_ids: &[&str]) -> Result<Vec<ShapePointRow>, RowsError> {
        self.with(|r| r.shape_points(shape_ids))
    }
    fn service_dates(
        &mut self,
        service_ids: &[&str],
        days: &[Date],
    ) -> Result<Vec<(String, Date)>, RowsError> {
        self.with(|r| r.service_dates(service_ids, days))
    }
}

// --- resident memory -------------------------------------------------------------------------

/// The highest resident memory seen, sampled every 50 ms on a thread of its own until dropped.
#[derive(Debug)]
pub struct PeakRss {
    peak: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl PeakRss {
    pub fn start() -> PeakRss {
        let peak = Arc::new(AtomicU64::new(rss_bytes().unwrap_or(0)));
        let stop = Arc::new(AtomicBool::new(false));
        let (p, s) = (Arc::clone(&peak), Arc::clone(&stop));
        let thread = std::thread::Builder::new()
            .name("peak-rss".into())
            .spawn(move || {
                while !s.load(Ordering::Relaxed) {
                    if let Some(now) = rss_bytes() {
                        p.fetch_max(now, Ordering::Relaxed);
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
            })
            .ok();
        PeakRss { peak, stop, thread }
    }

    pub fn peak(&self) -> Option<u64> {
        if let Some(now) = rss_bytes() {
            self.peak.fetch_max(now, Ordering::Relaxed);
        }
        Some(self.peak.load(Ordering::Relaxed)).filter(|p| *p > 0)
    }
}

impl Drop for PeakRss {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn mb(bytes: Option<u64>) -> Value {
    bytes.map_or(Value::Null, |b| {
        json!((b as f64 / 1_048_576.0 * 10.0).round() / 10.0)
    })
}

// --- container sizes -------------------------------------------------------------------------

/// The sizes of the containers that live as long as the worker, summed over feeds (Python's
/// `bench.sizes`, and every container the Rust worker adds).
pub fn sizes<L: Loads, B: Book>(
    runner: &Runner<L, Phases>,
    writer: &Writer<NullStore, B>,
    book: (usize, usize, usize),
    emitters: &BTreeMap<Feed, FeedEmitter>,
) -> BTreeMap<&'static str, usize> {
    let mut out: BTreeMap<&'static str, usize> = BTreeMap::new();
    let mut add = |name: &'static str, n: usize| *out.entry(name).or_default() += n;
    let mut longest = 0;
    for runtime in runner.runtimes.values() {
        let loader = &runtime.loader;
        let index = &loader.index;
        add("index_keys", index.key_entries.len());
        add("index_trips", index.trips.len());
        add(
            "index_calls",
            index.trips.values().map(|t| t.calls.len()).sum(),
        );
        add("index_services", index.trip_services.len());
        add("index_service_dates", index.service_dates.len());
        add("index_dates", index.loaded_dates.len());
        add("index_locations", index.locations.len());
        add("index_shapes", index.shapes.len());
        add("index_runs", index.runs.len());
        add("index_lines", index.lines.len());
        add("index_location_keys", index.location_keys.len());
        add("index_ids", index.pool.len());
        add("index_used", loader.used.len());
        add("index_lines_used", loader.lines_used.len());
        let (paths, plans) = runtime.plans.sizes();
        add("plan_paths", paths);
        add("plans", plans);
        let state = &runtime.state;
        add("vehicles", state.vehicles.len());
        add("instances", state.instances.len());
        add("dirty", state.dirty.len());
        add("hops", state.hops.len());
        add("hop_traversals", state.hops.values().map(|t| t.len()).sum());
        let mut placements = 0;
        for journey in state.instances.values() {
            let Some(track) = &journey.track else {
                continue;
            };
            for hypothesis in &track.hypotheses {
                placements += hypothesis.history.len();
                longest = longest.max(hypothesis.history.len());
            }
        }
        add("history_placements", placements);
    }
    out.insert("history_longest", longest);
    let (vehicles, trips, parts, calls) = writer.sizes();
    out.insert("rows_vehicles", vehicles);
    out.insert("rows_trips", trips);
    out.insert("rows_parts", parts);
    out.insert("rows_calls", calls);
    let (live, identities, members) = book;
    out.insert("book_live", live);
    out.insert("book_identities", identities);
    out.insert("book_members", members);
    out.insert(
        "emitter_trips",
        emitters.values().map(FeedEmitter::cached).sum(),
    );
    out
}

// --- the bench -------------------------------------------------------------------------------

#[derive(Debug, Clone, clap::Args)]
pub struct BenchArgs {
    #[command(flatten)]
    pub window: crate::compare::WindowArgs,
    /// Output directory: `bench.json`, `gtfs-rt/<feed>.pb`.
    #[arg(long)]
    pub out: PathBuf,
    /// Switch to another release on the way: `<release dir>@<ISO instant>` (repeatable), as
    /// the worker does when a release is activated.
    #[arg(long = "switch-release")]
    pub switch_release: Vec<String>,
    /// Sample memory and container sizes every this many simulated seconds.
    #[arg(long = "sample-every", default_value_t = 3600)]
    pub sample_every_s: i64,
    /// Run the first this many simulated hours as warm start does (no writer, no emitting
    /// ticks), then record what it left (`after_warm`) and go on as the worker.
    #[arg(long = "warm-hours", default_value_t = 0)]
    pub warm_hours: i64,
    /// The vehicle book forgets reports not observed for this many seconds (default: policy
    /// `emit.forget_reports_s`; 0: never, as Python).
    #[arg(long = "forget-reports")]
    pub forget_reports_s: Option<i64>,
    /// Keep the writer (and its vehicle book) across a release switch instead of opening a
    /// new one as the worker does.
    #[arg(long = "keep-writer")]
    pub keep_writer: bool,
}

struct Bench {
    writer: Writer<NullStore, VehicleBook>,
    emitters: BTreeMap<Feed, FeedEmitter>,
    gtfs_rt: PathBuf,
    policy: Arc<Policy>,
    ticks: Ticks,
    first: Option<Instant>,
    warm_hours: i64,
    warm_until: Option<Instant>,
    after_warm: Option<Value>,
    next_sample: Option<Instant>,
    sample_every: TimeDelta,
    samples: Vec<Value>,
    polls: u64,
    switches: Vec<(Instant, PathBuf)>,
    started: Monotonic,
    forget_reports_s: Option<i64>,
    keep_writer: bool,
    /// Reports forgotten by writers already replaced.
    forgotten: usize,
    /// During warm start: effects are not written.
    writing_paused: bool,
}

type BenchRunner = Runner<SharedLoads, Phases>;

/// The bench's writer of a release: rows dropped, the vehicle book's eviction after
/// `forget_s` (policy `emit.forget_reports_s` or `--forget-reports`).
fn bench_writer(
    policy: &Policy,
    run_id: &str,
    forget_s: Option<i64>,
) -> Result<Writer<NullStore, VehicleBook>, ReplayError> {
    let derivation = Derivation {
        core_version: CORE_VERSION.to_owned(),
        policy_version: policy.version.clone(),
        release_id: run_id.to_owned(),
    };
    let mut writer = Writer::open(NullStore, derivation, harness_book()?)
        .map_err(|e| ReplayError::Other(e.to_string()))?;
    writer.forget_reports(forget_s, policy.emit.sweep_index_every_s);
    Ok(writer)
}

fn states(runner: &BenchRunner) -> Vec<(Feed, &FeedState)> {
    runner
        .runtimes
        .iter()
        .map(|(feed, runtime)| (*feed, &runtime.state))
        .collect()
}

fn write_feed(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let temporary = path.with_extension("pb.partial");
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(&temporary, path)
}

impl Bench {
    fn warming(&self, at: Instant) -> bool {
        self.warm_until.is_some_and(|until| at < until)
    }

    fn write(&mut self, phases: &mut Phases, effects: &[Effect]) -> Result<(), ReplayError> {
        if effects.is_empty() || self.writing_paused {
            return Ok(());
        }
        phases
            .time("write", || self.writer.write(effects))
            .map_err(|e| ReplayError::Other(e.to_string()))
    }

    /// One emitting tick's output: its effects, the state tables, every feed's GTFS-RT file.
    fn ticked(
        &mut self,
        runner: &mut BenchRunner,
        now: Instant,
        effects: Vec<Effect>,
    ) -> Result<(), ReplayError> {
        let mut phases = std::mem::take(&mut runner.phases);
        let outcome = self.emit(runner, &mut phases, now, &effects);
        runner.phases = phases;
        outcome
    }

    fn emit(
        &mut self,
        runner: &BenchRunner,
        phases: &mut Phases,
        now: Instant,
        effects: &[Effect],
    ) -> Result<(), ReplayError> {
        self.write(phases, effects)?;
        // Before GTFS-RT: the state write settles who each report is (`VehicleBook`).
        phases
            .time("state", || self.writer.write_state(&states(runner)))
            .map_err(|e| ReplayError::Other(e.to_string()))?;
        for feed in runner.sorted_feeds() {
            let state = &runner.runtimes[&feed].state;
            let book = &self.writer.vehicles;
            let identity = |v: &obehy_core::model::VehicleId| book.vehicle_id(v);
            let emitter = self
                .emitters
                .entry(feed)
                .or_insert_with(|| FeedEmitter::new(self.policy.lifecycle.clone()));
            let path = self.gtfs_rt.join(format!("{}.pb", feed.as_str()));
            phases.time("gtfs_rt", || {
                let bytes = emitter.serialize::<&[u8]>(state, now, &[], &identity);
                write_feed(&path, &bytes)
            })?;
        }
        Ok(())
    }

    fn advance(&mut self, runner: &mut BenchRunner, at: Instant) -> Result<(), ReplayError> {
        if self.first.is_none() {
            self.first = Some(at);
            if self.warm_hours > 0 {
                let until = at + TimeDelta::from_seconds(self.warm_hours * 3600);
                self.warm_until = Some(until);
                self.writing_paused = true;
                self.next_sample = Some(until + self.sample_every);
            } else {
                self.next_sample = Some(at + self.sample_every);
            }
        }
        while self.switches.first().is_some_and(|(when, _)| *when <= at) {
            let (_, release) = self.switches.remove(0);
            let feeds: Vec<Feed> = runner.runtimes.keys().copied().collect();
            let loads = SharedLoads::open(&release, &feeds)?;
            eprintln!("bench: switching to release {} at {at}", loads.run_id);
            let mut phases = std::mem::take(&mut runner.phases);
            let run_id = loads.run_id.clone();
            let switched = phases.time("switch", || runner.switch_release(loads));
            if !self.keep_writer {
                // A writer of the new release, as the worker's `_writer`: a new vehicle book.
                self.forgotten += self.writer.forgotten;
                self.writer = bench_writer(&self.policy, &run_id, self.forget_reports_s)?;
            }
            let outcome = switched
                .map_err(ReplayError::from)
                .and_then(|effects| self.write(&mut phases, &effects));
            runner.phases = phases;
            outcome?;
        }
        let mut ticks = self.ticks.clone();
        ticks.advance(runner, at, |runner, now, effects| {
            if self.warming(now) {
                return Ok(()); // warm start: no writer, no emitting
            }
            self.ticked(runner, now, effects)?;
            Ok::<(), ReplayError>(())
        })?;
        self.ticks = ticks;
        if self.writing_paused && !self.warming(at) {
            // Warm start is over: what it left, then the worker's loop.
            self.writing_paused = false;
            let elapsed = self.started.elapsed().as_secs_f64();
            let mut record = self.record(runner, at);
            record.insert(
                "warm_elapsed_s".into(),
                json!((elapsed * 10.0).round() / 10.0),
            );
            eprintln!(
                "bench: warm start over at {at} in {elapsed:.1} s, rss {} MB",
                record["rss_mb"]
            );
            self.after_warm = Some(Value::Object(record));
        }
        if let Some(due) = self.next_sample
            && due <= at
        {
            self.sample(runner, due);
            self.next_sample = Some(due + self.sample_every);
        }
        Ok(())
    }

    fn sample(&mut self, runner: &BenchRunner, at: Instant) {
        let first = self.first.expect("started");
        let record = self.record(runner, at);
        eprintln!(
            "bench: {} +{:.0} h rss {} MB; vehicles {} instances {} index trips {} keys {} \
             plans {} book {} emitters {}",
            at,
            (at - first).total_seconds() / 3600.0,
            record["rss_mb"],
            record["vehicles"],
            record["instances"],
            record["index_trips"],
            record["index_keys"],
            record["plans"],
            record["book_live"],
            record["emitter_trips"],
        );
        self.samples.push(Value::Object(record));
    }

    fn record(&self, runner: &BenchRunner, at: Instant) -> serde_json::Map<String, Value> {
        let first = self.first.expect("started");
        let phases = &runner.phases;
        let mut record = serde_json::Map::new();
        record.insert("at".into(), json!(at.isoformat()));
        record.insert(
            "simulated_h".into(),
            json!(((at - first).total_seconds() / 36.0).round() / 100.0),
        );
        record.insert("polls".into(), json!(self.polls));
        record.insert("rss_mb".into(), mb(rss_bytes()));
        record.insert(
            "cpu_s".into(),
            json!((phases.cpu_s.values().sum::<f64>() * 100.0).round() / 100.0),
        );
        record.insert(
            "elapsed_s".into(),
            json!((self.started.elapsed().as_secs_f64() * 10.0).round() / 10.0),
        );
        record.insert(
            "forgotten_reports".into(),
            json!(self.forgotten + self.writer.forgotten),
        );
        let book = self.writer.vehicles.sizes();
        for (name, size) in sizes(runner, &self.writer, book, &self.emitters) {
            record.insert(name.into(), json!(size));
        }
        record
    }
}

fn parse_switch(text: &str) -> Result<(Instant, PathBuf), String> {
    let (release, at) = text
        .rsplit_once('@')
        .ok_or_else(|| format!("--switch-release is <release dir>@<instant>, not {text:?}"))?;
    Ok((crate::compare::local_time(at)?, PathBuf::from(release)))
}

fn share(seconds: f64, over_s: f64) -> Value {
    if over_s > 0.0 {
        json!((seconds / over_s * 10_000.0).round() / 10_000.0)
    } else {
        Value::Null
    }
}

/// `obehy-rt bench`.
pub fn run_command(args: &BenchArgs) -> ExitCode {
    match run(args) {
        Ok(document) => {
            for line in summary_lines(&document) {
                println!("{line}");
            }
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &BenchArgs) -> Result<Value, String> {
    let text = |e: &dyn std::fmt::Display| e.to_string();
    let Some(release) = &args.window.release else {
        return Err("bench needs --from-files <release dir> (no database bench)".into());
    };
    let setup = crate::compare::setup(&args.window)?;
    let feeds = crate::compare::parse_feeds(&args.window.feeds)?;
    let mut switches = args
        .switch_release
        .iter()
        .map(|s| parse_switch(s))
        .collect::<Result<Vec<_>, _>>()?;
    switches.sort();
    let gtfs_rt = args.out.join("gtfs-rt");
    std::fs::create_dir_all(&gtfs_rt).map_err(|e| text(&e))?;
    let peak = PeakRss::start();
    let rss_start = rss_bytes();
    let loads = SharedLoads::open(release, &feeds).map_err(|e| text(&e))?;
    let rss_tables = rss_bytes();
    let run_id = loads.run_id.clone();
    let skew = setup.policy.time.max_clock_skew();
    let policy = Arc::new(setup.policy.clone());
    let forget_s = args
        .forget_reports_s
        .or(policy.emit.forget_reports_s)
        .filter(|s| *s > 0);
    let mut runner = Runner::new(
        loads,
        policy.clone(),
        &feeds,
        crate::output::semantics_of(&setup.window),
        Phases::new(),
    )
    .map_err(|e| text(&e))?;
    let mut bench = Bench {
        writer: bench_writer(&policy, &run_id, forget_s).map_err(|e| text(&e))?,
        emitters: BTreeMap::new(),
        gtfs_rt,
        policy: policy.clone(),
        ticks: Ticks::new(&policy),
        first: None,
        warm_hours: args.warm_hours,
        warm_until: None,
        after_warm: None,
        next_sample: None,
        sample_every: TimeDelta::from_seconds(args.sample_every_s.max(1)),
        samples: Vec::new(),
        polls: 0,
        switches,
        started: Monotonic::now(),
        forget_reports_s: forget_s,
        keep_writer: args.keep_writer,
        forgotten: 0,
        writing_paused: false,
    };
    let times = DecodeTimes::default();
    let started = Monotonic::now();
    let mut last: Option<Instant> = None;
    decoded_polls(
        &setup.window,
        &setup.decoders,
        skew,
        args.window.threads,
        |poll| {
            bench.advance(&mut runner, poll.at)?;
            if let Some(observations) = &poll.observations
                && !observations.is_empty()
            {
                let effects = runner.process(observations)?;
                let mut phases = std::mem::take(&mut runner.phases);
                let outcome = bench.write(&mut phases, &effects);
                runner.phases = phases;
                outcome?;
            }
            bench.polls += 1;
            last = Some(poll.at);
            Ok(())
        },
        &times,
    )
    .map_err(|e| text(&e))?;
    let elapsed = started.elapsed().as_secs_f64();
    if let Some(last) = last {
        bench.sample(&runner, last);
    }
    runner.phases.add("decode", times.seconds(), 0.0);
    let simulated_s = match (bench.first, last) {
        (Some(first), Some(last)) => (last - first).total_seconds(),
        _ => 0.0,
    };
    let phases = &runner.phases;
    let observations = phases.counts.get("observations").copied().unwrap_or(0);
    let mut by_wall: Vec<&String> = phases.wall_s.keys().collect();
    by_wall.sort_by(|a, b| phases.wall_s[*b].total_cmp(&phases.wall_s[*a]));
    let mut phase_table = serde_json::Map::new();
    for name in by_wall {
        let cpu = phases.cpu_s[name];
        phase_table.insert(
            name.clone(),
            json!({
                "cpu_s": (cpu * 100.0).round() / 100.0,
                "wall_s": (phases.wall_s[name] * 100.0).round() / 100.0,
                "calls": phases.calls.get(name).copied().unwrap_or(0),
                "core_share_cpu": share(cpu, simulated_s),
                "cpu_us_per_observation": if observations > 0 {
                    json!((1e7 * cpu / observations as f64).round() / 10.0)
                } else {
                    Value::Null
                },
            }),
        );
    }
    let cpu: f64 = phases.cpu_s.values().sum();
    let wall: f64 = phases.wall_s.values().sum();
    let channels: Vec<String> = {
        let mut names: Vec<String> = setup
            .window
            .channels
            .iter()
            .map(|c| format!("{}/{}", c.source, c.channel))
            .collect();
        names.sort();
        names
    };
    let document = json!({
        "release": run_id,
        "from": setup.window.start.isoformat(),
        "to": setup.window.end.isoformat(),
        "channels": channels,
        "feeds": feeds.iter().map(|f| f.as_str()).collect::<Vec<_>>(),
        "polls": bench.polls,
        "simulated_s": simulated_s,
        "elapsed_s": (elapsed * 10.0).round() / 10.0,
        "observations": observations,
        "observations_per_s": if elapsed > 0.0 {
            json!((observations as f64 / elapsed).round())
        } else {
            Value::Null
        },
        "peak_rss_mb": mb(peak.peak()),
        "start_rss_mb": mb(rss_start),
        "tables_rss_mb": mb(rss_tables),
        "warm_hours": args.warm_hours,
        "after_warm": bench.after_warm.clone().unwrap_or(Value::Null),
        "allocator": crate::ALLOCATOR,
        "forget_reports_s": forget_s,
        "phases": Value::Object(phase_table),
        "core_share_cpu": share(cpu, simulated_s),
        "core_share_wall": share(wall, simulated_s),
        "samples": bench.samples,
    });
    let path = args.out.join("bench.json");
    let temporary = args.out.join(".partial-bench.json");
    std::fs::write(
        &temporary,
        serde_json::to_string_pretty(&document).map_err(|e| text(&e))? + "\n",
    )
    .map_err(|e| text(&e))?;
    std::fs::rename(&temporary, &path).map_err(|e| text(&e))?;
    Ok(document)
}

/// The summary `obehy rt bench` prints.
pub fn summary_lines(document: &Value) -> Vec<String> {
    let num = |v: &Value| v.as_f64().unwrap_or(0.0);
    let hours = num(&document["simulated_s"]) / 3600.0;
    let mut lines = vec![format!(
        "{} polls over {hours:.1} simulated h in {} s; {:.1}% of a core CPU, {:.1}% wall; \
         {} observations, {}/s; peak RSS {} MB ({})",
        document["polls"],
        document["elapsed_s"],
        100.0 * num(&document["core_share_cpu"]),
        100.0 * num(&document["core_share_wall"]),
        document["observations"],
        document["observations_per_s"],
        document["peak_rss_mb"],
        document["allocator"].as_str().unwrap_or("?"),
    )];
    if let Some(phases) = document["phases"].as_object() {
        for (name, phase) in phases {
            lines.push(format!(
                "  {name:<8} cpu {:>9.2} s  wall {:>9.2} s  ({} calls, {} us/observation)",
                num(&phase["cpu_s"]),
                num(&phase["wall_s"]),
                phase["calls"],
                phase["cpu_us_per_observation"],
            ));
        }
    }
    if let Some(samples) = document["samples"].as_array()
        && let (Some(first), Some(last)) = (samples.first(), samples.last())
    {
        for key in [
            "rss_mb",
            "index_trips",
            "plan_paths",
            "instances",
            "book_live",
            "emitter_trips",
        ] {
            lines.push(format!(
                "  {key}: {} at {} h -> {}",
                first[key], first["simulated_h"], last[key]
            ));
        }
    }
    lines
}
