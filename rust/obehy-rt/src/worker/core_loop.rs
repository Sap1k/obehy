//! The core's thread: the one place the runner, the writer and the GTFS-RT emitters live. Python
//! guards its runner with one asyncio lock shared by polls, ticks and rebases; here they are
//! messages on one queue, run in arrival order on a thread of its own (the core and the store
//! are synchronous), so the async side never blocks on the core.
//!
//! 1. Open the writer (it writes the sources' physical-vehicle policy, `rt.physical_vehicles`).
//! 2. Warm replay: stored observations of the last `warm_replay.max_hours` through the runner
//!    with no writer (`warm::warm_start`), while the pollers queue what they receive.
//! 3. Attach the writer, run the backlog the queue holds (ticks due before each poll first),
//!    publish the state tables and GTFS-RT files, and mark the worker ready.
//! 4. Serve messages until [`Message::Stop`]; then a last tick at the clock's time, emitted.
//!
//! Any error here is fatal: the thread ends, the worker stops and the process exits non-zero
//! (Python's poller ends on the same errors and takes the worker with it).

use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::sync::mpsc::Receiver;

use obehy_core::config::Policy;
use obehy_core::core::{CORE_VERSION, Semantics};
use obehy_core::model::{Derivation, Feed, Observation};
use obehy_core::publish::alerts::AlertEntities;
use obehy_core::runner::{Runner, Ticks};
use obehy_core::time::{Instant, next_multiple};
use obehy_core::vehicles::Vocabulary;
use tokio::sync::oneshot;
use tokio_postgres::Config;

use super::WorkerError;
use super::loads::{PgLoads, ReleaseLoads};
use super::memory::sizes_line;
use super::pipeline::{LiveWriter, Output, Pipeline};
use crate::fetch::Clock;
use crate::logs::Log;
use crate::phases::Phases;
use crate::scheduler::Stop;
use crate::store::vehicles::book;
use crate::store::{PgStore, Writer};
use crate::warm::{WarmError, stored_chunks, warm_start, warm_window};

/// Threads estimating journeys at a tick (warm start's ticks above all): the machine's, at
/// most [`MAX_ESTIMATE_THREADS`]. The output does not depend on it.
fn estimate_threads() -> usize {
    std::thread::available_parallelism().map_or(1, |n| n.get().min(MAX_ESTIMATE_THREADS))
}

const MAX_ESTIMATE_THREADS: usize = 4;

/// The outcome the async side waits for.
pub type Reply = Result<(), String>;

#[derive(Debug)]
pub enum Message {
    /// A facts channel's poll received at `at`; `done` when the poller waits for it.
    Poll {
        at: Instant,
        observations: Vec<Arc<Observation>>,
        done: Option<oneshot::Sender<Reply>>,
    },
    /// The wall clock passed the tick at `at` with no poll after it yet.
    Tick {
        at: Instant,
        done: oneshot::Sender<Reply>,
    },
    /// An alert poll changed `rt.alert`.
    AlertsChanged,
    /// `obehy release activate` published another release.
    Switch {
        loads: ReleaseLoads,
        done: oneshot::Sender<Reply>,
    },
    /// `obehy ref import` loaded a vehicle dataset.
    ReloadRegistry {
        done: oneshot::Sender<Reply>,
    },
    /// The summary's core lines: phases since the last summary (then reset) and sizes.
    Summary {
        reply: oneshot::Sender<(Phases, String)>,
    },
    Stop,
}

/// What the core thread is started with.
#[derive(Clone)]
pub struct CoreSetup {
    pub database_url: String,
    pub config: Config,
    pub loads: ReleaseLoads,
    pub policy: Arc<Policy>,
    pub feeds: Vec<Feed>,
    pub semantics: Semantics,
    pub gtfs_rt_dir: std::path::PathBuf,
    pub physical: Vec<(String, String, bool)>,
    pub vocabulary: Vocabulary,
    pub clock: Clock,
    pub log: Log,
    /// Set by the worker's stop: warm replay ends early.
    pub warm_stop: Arc<AtomicBool>,
    pub ready: Stop,
}

impl std::fmt::Debug for CoreSetup {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CoreSetup")
            .field("loads", &self.loads)
            .field("feeds", &self.feeds)
            .finish_non_exhaustive()
    }
}

/// The writer of `loads`' release (a new one at each switch, as Python's `_writer`).
pub fn open_writer(setup: &CoreSetup, loads: &ReleaseLoads) -> Result<LiveWriter, WorkerError> {
    let store = PgStore::connect_config(&setup.config, setup.physical.clone())?;
    let derivation = Derivation {
        core_version: CORE_VERSION.to_owned(),
        policy_version: setup.policy.version.clone(),
        release_id: loads.run_id.clone(),
    };
    let vocabulary = setup.vocabulary.clone();
    let mut writer = Writer::open(store, derivation, |stored| book(stored, vocabulary))?;
    let emit = &setup.policy.emit;
    writer.forget_reports(emit.forget_reports_s, emit.sweep_index_every_s);
    Ok(writer)
}

fn answer(done: Option<oneshot::Sender<Reply>>, result: &Result<(), WorkerError>) {
    if let Some(done) = done {
        let _ = done.send(result.as_ref().map(|_| ()).map_err(|e| e.to_string()));
    }
}

/// Run the core until stopped; the pipeline as it ended (tests read it).
pub fn run_core(
    setup: CoreSetup,
    messages: Receiver<Message>,
) -> Result<Pipeline<PgLoads>, WorkerError> {
    let log = Arc::clone(&setup.log);
    let loads = PgLoads {
        config: setup.config.clone(),
        loads: setup.loads.clone(),
    };
    let mut runner = Runner::new(
        loads,
        Arc::clone(&setup.policy),
        &setup.feeds,
        setup.semantics.clone(),
        Phases::new(),
    )?;
    runner.estimate_threads = estimate_threads();
    let writer = open_writer(&setup, &setup.loads)?;
    let mut pipeline = Pipeline {
        runner,
        output: Output {
            writer: None,
            gtfs_rt_dir: Some(setup.gtfs_rt_dir.clone()),
            lifecycle: setup.policy.lifecycle.clone(),
            emitters: Default::default(),
            alerts: Some(AlertEntities::new()),
        },
        ticks: Ticks::new(&setup.policy),
    };

    // Warm replay, on the replay clock.
    let until = (setup.clock)();
    let every_s = setup.policy.emit.tick_s;
    let chunks = stored_chunks(&setup.database_url, warm_window(&setup.policy, until));
    let warmed = warm_start(
        &mut pipeline.runner,
        chunks,
        until,
        &setup.warm_stop,
        Some(&|line: &str| log(line)),
    )
    .map_err(|e| match e {
        WarmError::Run(e) => WorkerError::Run(e),
        WarmError::Store(e) => WorkerError::Store(e),
    })?;
    if warmed > 0 {
        // Warm replay ran every tick up to `until`: live ticks go on from the next one.
        pipeline.ticks.next = Some(next_multiple(until, every_s));
    }

    // Live: the writer attached, the backlog run, the state published.
    pipeline.output.writer = Some(writer);
    let mut backlog = 0;
    let mut stopped = false;
    while let Ok(message) = messages.try_recv() {
        if let Message::Poll { observations, .. } = &message {
            backlog += observations.len();
        }
        if !serve(&setup, &mut pipeline, message)? {
            stopped = true; // stopped during warm replay: the backlog was all there is
            break;
        }
    }
    pipeline
        .output
        .emit(&mut pipeline.runner, (setup.clock)())?;
    pipeline.runner.phases.reset();
    setup.ready.set();
    log(&format!(
        "realtime worker on release {}; warm replay of {warmed} observations, {backlog} more \
         received meanwhile",
        setup.loads.run_id
    ));
    while !stopped && let Ok(message) = messages.recv() {
        stopped = !serve(&setup, &mut pipeline, message)?;
    }
    finish(&setup, pipeline)
}

fn finish(
    setup: &CoreSetup,
    mut pipeline: Pipeline<PgLoads>,
) -> Result<Pipeline<PgLoads>, WorkerError> {
    pipeline.close((setup.clock)())?;
    Ok(pipeline)
}

/// One message; false on [`Message::Stop`]. An error is answered to a waiting sender and then
/// ends the core.
fn serve(
    setup: &CoreSetup,
    pipeline: &mut Pipeline<PgLoads>,
    message: Message,
) -> Result<bool, WorkerError> {
    let (result, done) = match message {
        Message::Stop => return Ok(false),
        Message::Poll {
            at,
            observations,
            done,
        } => (pipeline.poll(at, &observations), done),
        Message::Tick { at, done } => (pipeline.advance(at), Some(done)),
        Message::AlertsChanged => {
            if let Some(alerts) = &mut pipeline.output.alerts {
                alerts.dirty = true;
            }
            return Ok(true);
        }
        Message::Switch { loads, done } => {
            let result = open_writer(setup, &loads).and_then(|writer| {
                let fresh = PgLoads {
                    config: setup.config.clone(),
                    loads,
                };
                pipeline.switch_release(fresh, writer)
            });
            (result, Some(done))
        }
        Message::ReloadRegistry { done } => {
            let result = match &mut pipeline.output.writer {
                Some(writer) => writer.reload_registry().map_err(WorkerError::from),
                None => Ok(()),
            };
            (result, Some(done))
        }
        Message::Summary { reply } => {
            let phases = std::mem::take(&mut pipeline.runner.phases);
            let _ = reply.send((phases, sizes_line(pipeline)));
            return Ok(true);
        }
    };
    answer(done, &result);
    result.map(|()| true)
}
