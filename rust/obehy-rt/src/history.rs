//! `obehy-rt replay --write-history`: rebuild `rt.observation` and history of the replayed days
//! in PostgreSQL (`replay.history_writer` and `replay.replay` with `write_history`).
//!
//! The observations of the replayed UTC days of the window's sources and the history of those
//! service dates are cleared first; then the window runs through the runner as replay runs it,
//! each step's and tick's effects written by the worker's writer into [`PgStore`] (one
//! transaction each, as the runner writes), and after the last poll the final tick's effects
//! and the current-state tables. Ticks do not emit, as in Python's replay: the state tables are
//! written once, at the end.

use std::process::ExitCode;
use std::sync::Arc;

use obehy_core::core::CORE_VERSION;
use obehy_core::model::{Derivation, Effect, Feed, FeedState};
use obehy_core::runner::{Runner, Ticks};
use obehy_core::time::{Date, Instant};
use obehy_core::vehicles::VehicleBook;

use crate::output::harness_book;
use crate::phases::Phases;
use crate::replay::{
    Clock, DecodeTimes, DecodedPoll, FileLoads, ReplayArgs, ReplayError, Sink, decoded_polls,
    drive, finish,
};
use crate::store::pg::clear_observations;
use crate::store::vehicles::physical_rows;
use crate::store::{PgStore, Writer};

type Timed = Runner<FileLoads, Phases>;

/// Writes every step's and tick's effects.
struct History {
    writer: Writer<PgStore, VehicleBook>,
    polls: u64,
    writes: u64,
}

impl History {
    fn write(&mut self, runner: &mut Timed, effects: &[Effect]) -> Result<(), ReplayError> {
        if effects.is_empty() {
            return Ok(());
        }
        self.writes += 1;
        runner
            .phases
            .time("write", || self.writer.write(effects))
            .map_err(|e| ReplayError::Other(e.to_string()))
    }
}

impl Sink<FileLoads, Phases> for History {
    fn stepped(
        &mut self,
        runner: &mut Timed,
        _poll: &DecodedPoll,
        effects: Option<Vec<Effect>>,
    ) -> Result<(), ReplayError> {
        self.polls += 1;
        match effects {
            Some(effects) => self.write(runner, &effects),
            None => Ok(()),
        }
    }

    fn ticked(
        &mut self,
        runner: &mut Timed,
        _at: Instant,
        effects: Vec<Effect>,
    ) -> Result<(), ReplayError> {
        self.write(runner, &effects)
    }

    fn snapshot(&mut self, _runner: &mut Timed, _at: Instant) -> Result<(), ReplayError> {
        Ok(())
    }
}

fn states(runner: &Timed) -> Vec<(Feed, &FeedState)> {
    runner
        .runtimes
        .iter()
        .map(|(feed, runtime)| (*feed, &runtime.state))
        .collect()
}

/// `obehy-rt replay --write-history`.
pub fn run_command(args: &ReplayArgs) -> ExitCode {
    match run(args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &ReplayArgs) -> Result<(), String> {
    let text = |e: &dyn std::fmt::Display| e.to_string();
    let release = args
        .window
        .release
        .as_ref()
        .ok_or("replay needs --from-files <release dir>")?;
    let setup = crate::compare::setup(&args.window)?;
    let feeds = crate::compare::parse_feeds(&args.window.feeds)?;
    let paths = crate::settings::Paths::discover().map_err(|e| text(&e))?;
    let url = match &args.url_env {
        Some(name) => std::env::var(name).map_err(|e| format!("{name}: {e}"))?,
        None => crate::settings::load_database_url(&paths.config(), &crate::settings::ProcessEnv)
            .map_err(|e| text(&e))?,
    };
    let loads = FileLoads::open(release).map_err(|e| text(&e))?;
    let derivation = Derivation {
        core_version: CORE_VERSION.to_owned(),
        policy_version: setup.policy.version.clone(),
        release_id: loads.run_id.clone(),
    };
    let store = PgStore::connect(&url, physical_rows(&setup.sources)).map_err(|e| text(&e))?;
    let mut writer = Writer::open(store, derivation, harness_book().map_err(|e| text(&e))?)
        .map_err(|e| text(&e))?;
    writer.forget_reports(
        setup.policy.emit.forget_reports_s,
        setup.policy.emit.sweep_index_every_s,
    );
    // `history_writer`: the replayed days' observations of these sources and their service
    // dates' history go first.
    let (start, end) = (setup.window.start, setup.window.end);
    let mut sources: Vec<&str> = setup
        .window
        .channels
        .iter()
        .map(|c| c.source.as_str())
        .collect();
    sources.sort_unstable();
    sources.dedup();
    let midnight = |day: Date| Instant::from_utc(day.midnight());
    let cleared = writer
        .store
        .block_on(clear_observations(
            writer.store.client(),
            midnight(start),
            midnight(end.add_days(1)),
            &sources,
        ))
        .map_err(|e| text(&e))?;
    let days: Vec<Date> = (0..=end.days_since(start))
        .map(|n| start.add_days(n))
        .collect();
    writer.clear_history(days).map_err(|e| text(&e))?;
    let skew = setup.policy.time.max_clock_skew();
    let policy = Arc::new(setup.policy.clone());
    let mut runner = Runner::new(
        loads,
        policy.clone(),
        &feeds,
        crate::output::semantics_of(&setup.window),
        Phases::new(),
    )
    .map_err(|e| text(&e))?;
    runner.estimate_threads = args.window.threads.max(1);
    let mut sink = History {
        writer,
        polls: 0,
        writes: 0,
    };
    let mut clock = Clock::new(Ticks::new(&policy), None);
    let times = DecodeTimes::default();
    let started = std::time::Instant::now();
    let last = drive(
        &mut runner,
        |each| {
            decoded_polls(
                &setup.window,
                &setup.decoders,
                skew,
                args.window.threads,
                each,
                &times,
            )
        },
        &mut clock,
        &mut sink,
    )
    .map_err(|e| text(&e))?;
    if let Some(last) = last {
        let effects = finish(&mut runner, last).map_err(|e| text(&e))?;
        sink.write(&mut runner, &effects).map_err(|e| text(&e))?;
        let mut phases = std::mem::take(&mut runner.phases);
        phases
            .time("state", || sink.writer.write_state(&states(&runner)))
            .map_err(|e| text(&e))?;
        runner.phases = phases;
    }
    let wall = started.elapsed().as_secs_f64();
    let observations = runner
        .phases
        .counts
        .get("observations")
        .copied()
        .unwrap_or(0);
    let phases: Vec<String> = runner
        .phases
        .cpu_s
        .iter()
        .map(|(name, cpu)| format!("{name} {cpu:.2}"))
        .collect();
    println!(
        "{} polls, {observations} observations, {} writes in {wall:.1} s ({:.0} observations/s); \
         {cleared} stored observations cleared first",
        sink.polls,
        sink.writes,
        observations as f64 / wall.max(1e-9)
    );
    println!(
        "cpu s: {}, decode {:.2} (all threads)",
        phases.join(", "),
        times.seconds()
    );
    Ok(())
}
