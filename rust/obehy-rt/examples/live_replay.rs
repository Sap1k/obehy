//! Replay of a live worker's archive into a database, as `tests/worker_db.rs` replays (R8 live
//! check): the database's active release (a fresh copy of the worker's), the runner over its
//! index, every step's effects written, every tick written with the state tables and GTFS-RT,
//! a last tick at the last poll. Then `rust/tools/store_db.py dump` compares the two databases.
//!
//! ```text
//! cargo run --release -p obehy-rt --example live_replay -- --archive DIR --day 2026-11-21 \
//!     --sources duk,dpmd --feeds jdf --gtfs-rt DIR        # URL in OBEHY_REPLAY_URL
//! ```

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use obehy_core::config::manifest::{RECORDER_FILTERS, load_sources, select_channels};
use obehy_core::core::CORE_VERSION;
use obehy_core::model::Derivation;
use obehy_core::publish::alerts::AlertEntities;
use obehy_core::runner::{Runner, Ticks};
use obehy_core::time::Date;
use obehy_core::vehicles::Vocabulary;
use obehy_core::vehicles::vocabulary::VOCABULARY_FILE;
use obehy_rt::compare::{Window, parse_feeds};
use obehy_rt::phases::Phases;
use obehy_rt::replay::{self, DecodeTimes, decoded_polls, drive, finish};
use obehy_rt::settings::Paths;
use obehy_rt::store::vehicles::{book, physical_rows};
use obehy_rt::store::{PgStore, Writer};
use obehy_rt::worker::loads::{PgLoads, active_loads};
use obehy_rt::worker::pipeline::{Output, ReplaySink};
use obehy_rt::worker::semantics_of;
use obehy_sources::Decoders;
use tokio_postgres::{Config, NoTls};

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1).cloned())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let archive = PathBuf::from(arg(&args, "--archive").ok_or("--archive")?);
    let day = Date::from_isoformat(&arg(&args, "--day").ok_or("--day")?)?;
    let gtfs_rt = PathBuf::from(arg(&args, "--gtfs-rt").ok_or("--gtfs-rt")?);
    let feeds = parse_feeds(&arg(&args, "--feeds").unwrap_or_else(|| "jdf,czptt".into()))?;
    let wanted = arg(&args, "--sources").unwrap_or_default();
    let wanted: Vec<&str> = wanted.split(',').filter(|s| !s.is_empty()).collect();
    let config: Config = std::env::var("OBEHY_REPLAY_URL")?.parse()?;

    let paths = Paths::discover()?;
    let sources = load_sources(&paths.sources(), &RECORDER_FILTERS)?;
    let all: Vec<_> = sources.iter().flat_map(|s| s.channels.clone()).collect();
    let channels = select_channels(&all, &wanted)?;
    let package = paths.root.join("src/obehy");
    let files = |name: &str| std::fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    let decoders = Decoders::new(&sources, &files)?;
    let vocabulary = Vocabulary::load(&paths.data().join("..").join(VOCABULARY_FILE))?;
    let policy = Arc::new(paths.load_policy()?);

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let loads = runtime.block_on(async {
        let (client, connection) = config.connect(NoTls).await?;
        tokio::spawn(connection);
        active_loads(&client)
            .await
            .map_err(|e| Box::<dyn std::error::Error>::from(e.to_string()))
    })?;
    drop(runtime);
    let started = std::time::Instant::now();
    let mut runner = Runner::new(
        PgLoads {
            config: config.clone(),
            loads: loads.clone(),
        },
        Arc::clone(&policy),
        &feeds,
        semantics_of(&channels),
        Phases::new(),
    )?;
    let store = PgStore::connect_config(&config, physical_rows(&sources))?;
    let derivation = Derivation {
        core_version: CORE_VERSION.to_owned(),
        policy_version: policy.version.clone(),
        release_id: loads.run_id.clone(),
    };
    let writer = Writer::open(store, derivation, |stored| book(stored, vocabulary))?;
    let mut output = Output {
        writer: Some(writer),
        gtfs_rt_dir: Some(gtfs_rt),
        lifecycle: policy.lifecycle.clone(),
        emitters: Default::default(),
        alerts: Some(AlertEntities::new()),
    };
    let window = Window {
        archive,
        start: day,
        end: day,
        channels: channels.clone(),
        since: None,
        until: None,
        all_feeds: false,
    };
    let mut clock = replay::Clock::new(Ticks::new(&policy), None);
    let times = DecodeTimes::default();
    let skew = policy.time.max_clock_skew();
    let last = drive(
        &mut runner,
        |each| decoded_polls(&window, &decoders, skew, 1, each, &times),
        &mut clock,
        &mut ReplaySink {
            output: &mut output,
        },
    )?
    .ok_or("no polls replayed")?;
    let effects = finish(&mut runner, last)?;
    output.ticked(&mut runner, last, &effects)?;
    println!(
        "replayed into release {} up to {last} in {:.1} s",
        loads.run_id,
        started.elapsed().as_secs_f64()
    );
    Ok(())
}
