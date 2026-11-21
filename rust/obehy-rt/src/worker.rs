//! `obehy-rt worker`: the long-running realtime worker, port of `realtime/worker.py`
//! (BASE_PLAN.md section 18.3, docs/RUST_PORT.md R8).
//!
//! What runs where:
//!
//! - **The core thread** ([`core_loop`]): the runner, the writer, the vehicle book, the GTFS-RT
//!   emitters and the alert entities. It warm-replays the stored observations first, then serves
//!   messages in arrival order: polls, ticks, release switches, registry reloads, summaries.
//! - **Pollers** (tokio tasks, one per channel; demand channels read station boards while trains
//!   are due): fetch through egress, then archive and decode on the blocking pool
//!   ([`handler`]) and hand the observations to the core. An alert channel's alerts are written
//!   to `rt.alert` on a connection of their own and republished by the next emitting tick.
//! - **Ticks** on round multiples of `emit.tick_s`: every poll first runs the ticks due before
//!   it (so live, warm start and replay tick at the same instants relative to observations); a
//!   timer runs the tick when the wall clock passed it with no poll after it. Each tick writes
//!   its effects, the state tables and every feed's `<gtfs-rt>/<feed>.pb` (atomically).
//! - **Publications**: `NOTIFY obehy_publication` (`obehy release activate`) rebases live
//!   journeys onto the new release under a new writer; `NOTIFY obehy_registry` (`obehy ref
//!   import`) reloads the vehicle registry.
//! - **The lookup broker** (compositions) on its own connection, never through the core.
//! - **Summaries** every ten minutes: each channel's polls, the phases' share of a core, and the
//!   memory line (resident memory and every long-lived container's size).
//!
//! Before any of it the worker refuses a database whose schema it does not know ([`schema`]).
//! SIGTERM or Ctrl-C stops the pollers after their poll in flight; the core runs what it was
//! given, ticks once more and publishes, and the process exits.

pub mod command;
pub mod core_loop;
pub mod handler;
pub mod loads;
pub mod memory;
pub mod pipeline;
pub mod schema;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use obehy_core::config::{Channel, Lookup, Policy, Source};
use obehy_core::core::Semantics;
use obehy_core::index::RowsError;
use obehy_core::model::Feed;
use obehy_core::runner::RunError;
use obehy_core::time::next_multiple;
use obehy_core::vehicles::Vocabulary;
use obehy_sources::Decoders;
use tokio::sync::oneshot;
use tokio::task::JoinSet;
use tokio_postgres::{Client, Config, NoTls};

use crate::archive::{ArchiveWriter, Poll};
use crate::demand::{DueCalls, DueSource, StationDemand, full_codes, run_demand};
use crate::fetch::Clock;
use crate::logs::Log;
use crate::lookups::{LookupBroker, SWEEP_S, listen, run_broker};
use crate::phases::Phases;
use crate::record::{ChannelStats, Filters, SUMMARY_INTERVAL_S};
use crate::scheduler::{Fetcher, HandlerError, Stop, run_channel};
use crate::store::StoreError;
use crate::store::vehicles::physical_rows;
use core_loop::{CoreSetup, Message, Reply, run_core};
use handler::{PollHandler, Shared};
use loads::active_loads;
use schema::check_schema;

/// `obehy release activate` notifies this channel.
pub const PUBLICATION_CHANNEL: &str = "obehy_publication";
/// `obehy ref import` notifies this channel (`reference.registry.REGISTRY_CHANNEL`).
pub const REGISTRY_CHANNEL: &str = "obehy_registry";
/// The timer runs a tick this long after the wall clock passed it, so a poll received just
/// before it, still being decoded, runs first.
pub const TICK_GRACE_S: f64 = 1.0;

#[derive(Debug, thiserror::Error)]
pub enum WorkerError {
    #[error(transparent)]
    Run(#[from] RunError),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error("database: {0}")]
    Database(#[from] tokio_postgres::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Schema(String),
    #[error("{0}")]
    Other(String),
}

/// Also a poll handler's error (`HandlerError` is the same boxed error).
impl From<RowsError> for WorkerError {
    fn from(error: RowsError) -> WorkerError {
        WorkerError::Other(error.to_string())
    }
}

/// Everything the worker runs with (`WorkerOptions` and what `commands._worker` loads).
#[derive(Clone)]
pub struct WorkerOptions {
    pub database_url: String,
    /// The channels to poll (egress-usable).
    pub channels: Vec<Channel>,
    /// The lookups the broker answers (egress-usable).
    pub lookups: Vec<Lookup>,
    /// Every manifest: the physical-vehicle policy is written for all of them.
    pub sources: Vec<Source>,
    pub decoders: Arc<Decoders>,
    pub policy: Arc<Policy>,
    pub vocabulary: Vocabulary,
    /// The SR70 catalogue (demand channels' station codes).
    pub catalogue: PathBuf,
    pub feeds: Vec<Feed>,
    pub archive: PathBuf,
    pub gtfs_rt_dir: PathBuf,
    /// Poll every channel once, run what came in and stop (tests): no ticks timer, summaries,
    /// publications or broker.
    pub once: bool,
    pub clock: Clock,
    pub log: Log,
}

impl std::fmt::Debug for WorkerOptions {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WorkerOptions")
            .field("channels", &self.channels.len())
            .field("feeds", &self.feeds)
            .field("archive", &self.archive)
            .field("gtfs_rt_dir", &self.gtfs_rt_dir)
            .finish_non_exhaustive()
    }
}

/// The channels' core semantics (`semantics_by_channel`).
pub fn semantics_of(channels: &[Channel]) -> Semantics {
    channels
        .iter()
        .map(|c| ((c.source.clone(), c.channel.clone()), c.core_semantics))
        .collect()
}

async fn connect(config: &Config, what: &'static str) -> Result<Client, tokio_postgres::Error> {
    let (client, connection) = config.connect(NoTls).await?;
    tokio::spawn(async move {
        if let Err(error) = connection.await {
            crate::logs::log(&format!("{what} database connection: {error}"));
        }
    });
    Ok(client)
}

/// The demand source shared by the board poller and the publications task (which switches it
/// to a new release).
#[derive(Debug, Clone)]
struct SharedDemand(Arc<tokio::sync::Mutex<StationDemand>>);

impl DueSource for SharedDemand {
    async fn due(&mut self, now: obehy_core::time::Instant) -> Result<DueCalls, HandlerError> {
        self.0.lock().await.due(now).await
    }
}

/// One fetcher shared by the pollers and the broker.
struct SharedFetcher<F>(Arc<F>);

impl<F: Fetcher> Fetcher for SharedFetcher<F> {
    fn fetch(
        &self,
        channel: &Channel,
        body: Option<Vec<u8>>,
    ) -> impl std::future::Future<Output = Poll> + Send {
        self.0.fetch(channel, body)
    }
}

/// Ask the core and wait for it.
async fn ask(
    core: &std::sync::mpsc::Sender<Message>,
    message: impl FnOnce(oneshot::Sender<Reply>) -> Message,
) -> Result<(), HandlerError> {
    let (done, wait) = oneshot::channel();
    core.send(message(done))
        .map_err(|_| "the core has stopped")?;
    wait.await.map_err(|_| "the core has stopped")??;
    Ok(())
}

/// Run the worker until `stop` (or, with `once`, one poll per channel).
pub async fn run_worker<F: Fetcher>(
    options: WorkerOptions,
    fetcher: Arc<F>,
    stop: Stop,
) -> Result<(), WorkerError> {
    let log = Arc::clone(&options.log);
    let config: Config = options.database_url.parse()?;
    let client = connect(&config, "worker").await?;
    check_schema(&client).await?;
    let loads = active_loads(&client).await?;

    let ready = Stop::new();
    let warm_stop = Arc::new(AtomicBool::new(false));
    let (core, messages) = std::sync::mpsc::channel::<Message>();
    let setup = CoreSetup {
        database_url: options.database_url.clone(),
        config: config.clone(),
        loads: loads.clone(),
        policy: Arc::clone(&options.policy),
        feeds: options.feeds.clone(),
        semantics: semantics_of(&options.channels),
        gtfs_rt_dir: options.gtfs_rt_dir.clone(),
        physical: physical_rows(&options.sources),
        vocabulary: options.vocabulary.clone(),
        clock: Arc::clone(&options.clock),
        log: Arc::clone(&log),
        warm_stop: Arc::clone(&warm_stop),
        ready: ready.clone(),
    };
    let core_stop = stop.clone();
    let core_thread = std::thread::Builder::new()
        .name("core".into())
        .spawn(move || {
            // Dropped here: its connections' runtimes may not be dropped on the async side.
            let result = run_core(setup, messages).map(drop);
            if result.is_err() {
                core_stop.set(); // the pollers end too
            }
            result
        })?;
    {
        let (stop, warm_stop) = (stop.clone(), Arc::clone(&warm_stop));
        tokio::spawn(async move {
            stop.wait().await;
            warm_stop.store(true, Ordering::SeqCst);
        });
    }

    let alert_channels = options
        .channels
        .iter()
        .any(|c| options.decoders.is_alert_channel(&c.source, &c.channel));
    let alerts = match alert_channels {
        true => Some(connect(&config, "alerts").await?),
        false => None,
    };
    let stats: IndexMap<String, ChannelStats> = options
        .channels
        .iter()
        .map(|c| (c.name(), ChannelStats::default()))
        .collect();
    let handler = Arc::new(PollHandler {
        archive: ArchiveWriter::new(&options.archive),
        decoders: Arc::clone(&options.decoders),
        filters: Filters::shipped(),
        skew: options.policy.time.max_clock_skew(),
        stats: Mutex::new(stats),
        phases: Mutex::new(Phases::new()),
        core: core.clone(),
        ready: ready.clone(),
        alerts,
        log: Arc::clone(&log),
    });
    let demand = if options.channels.iter().any(|c| c.demand.is_some()) {
        let client = Arc::new(connect(&config, "demand").await?);
        let codes = full_codes(&options.catalogue)?;
        let source = StationDemand::new(client, loads.czptt, options.policy.boards.clone(), codes);
        Some(SharedDemand(Arc::new(tokio::sync::Mutex::new(source))))
    } else {
        None
    };

    let mut helpers: JoinSet<()> = JoinSet::new();
    if !options.once {
        let every_s = options.policy.emit.tick_s;
        {
            let (core, ready, stop, clock) = (
                core.clone(),
                ready.clone(),
                stop.clone(),
                Arc::clone(&options.clock),
            );
            let failed = Arc::clone(&log);
            helpers.spawn(async move {
                if let Err(error) = ticks(&core, &ready, &stop, &clock, every_s).await {
                    failed(&format!("ticks: {error}"));
                    stop.set();
                }
            });
        }
        helpers.spawn(summaries(
            core.clone(),
            ready.clone(),
            stop.clone(),
            Arc::clone(&handler),
            Arc::clone(&log),
        ));
        {
            let (core, stop, log_to, demand) =
                (core.clone(), stop.clone(), Arc::clone(&log), demand.clone());
            let (config, run_id) = (config.clone(), loads.run_id.clone());
            helpers.spawn(async move {
                if let Err(error) =
                    publications(&config, &core, &stop, run_id, demand, &log_to).await
                {
                    log_to(&format!("publications: {error}"));
                }
            });
        }
        if !options.lookups.is_empty() {
            let broker_client = connect(&config, "lookup broker").await?;
            let started = std::time::Instant::now();
            let mut broker = LookupBroker::new(
                broker_client,
                &options.lookups,
                SharedFetcher(Arc::clone(&fetcher)),
                ArchiveWriter::new(&options.archive),
                Arc::clone(&options.clock),
                Box::new(move || started.elapsed().as_secs_f64()),
                Arc::clone(&log),
                options.policy.journey.clone(),
            );
            let (config, stop, log_to) = (config.clone(), stop.clone(), Arc::clone(&log));
            helpers.spawn(async move {
                if let Err(error) = run_broker(&mut broker, &config, &stop, SWEEP_S).await {
                    log_to(&format!("lookup broker: {error}"));
                }
            });
        }
    }

    let mut pollers: JoinSet<Result<(), HandlerError>> = JoinSet::new();
    for channel in options.channels.iter().cloned() {
        let channel = Arc::new(channel);
        let on_poll = Arc::new(Shared(Arc::clone(&handler)));
        let (stop, fetcher, clock) = (
            stop.clone(),
            Arc::clone(&fetcher),
            Arc::clone(&options.clock),
        );
        let once = options.once;
        match (&channel.demand, &demand) {
            (None, _) => {
                pollers.spawn(run_channel(channel, stop, fetcher, on_poll, clock, once));
            }
            (Some(_), Some(demand)) => {
                let (mut source, boards) = (demand.clone(), options.policy.boards.clone());
                pollers.spawn(async move {
                    run_demand(
                        channel,
                        stop,
                        &mut source,
                        fetcher,
                        on_poll,
                        &boards,
                        clock,
                        None,
                        once,
                    )
                    .await
                    .map(|_| ())
                });
            }
            (Some(_), None) => unreachable!("a demand channel has its demand source"),
        }
    }
    let mut failure: Option<WorkerError> = None;
    while let Some(joined) = pollers.join_next().await {
        let result = joined
            .map_err(|e| -> HandlerError { Box::new(e) })
            .and_then(|r| r);
        if let Err(error) = result {
            failure.get_or_insert(WorkerError::Other(error.to_string()));
            stop.set();
        }
    }
    // The core runs what it was given, ticks once more and publishes.
    let _ = core.send(Message::Stop);
    let ended = tokio::task::spawn_blocking(move || core_thread.join())
        .await
        .map_err(|e| WorkerError::Other(e.to_string()))?
        .map_err(|_| WorkerError::Other("the core thread panicked".into()))?;
    stop.set();
    helpers.abort_all();
    for (name, channel_stats) in handler.stats.lock().expect("stats").iter() {
        log(&channel_stats.line(name));
    }
    match (ended, failure) {
        (Err(error), _) => Err(error),
        (Ok(_), Some(error)) => Err(error),
        (Ok(()), None) => Ok(()),
    }
}

/// Run each tick once the wall clock passed it with no poll after it (a poll runs the ticks
/// before it itself).
async fn ticks(
    core: &std::sync::mpsc::Sender<Message>,
    ready: &Stop,
    stop: &Stop,
    clock: &Clock,
    every_s: i64,
) -> Result<(), HandlerError> {
    tokio::select! {
        () = ready.wait() => {}
        () = stop.wait() => return Ok(()),
    }
    while !stop.is_set() {
        let now = clock();
        let next = next_multiple(now, every_s);
        stop.sleep((next - now).total_seconds() + TICK_GRACE_S)
            .await;
        if stop.is_set() || clock() < next {
            continue;
        }
        ask(core, |done| Message::Tick { at: next, done }).await?;
    }
    Ok(())
}

/// Every [`SUMMARY_INTERVAL_S`]: each channel's line, the phases' share of a core since the
/// last summary, and the memory line.
async fn summaries(
    core: std::sync::mpsc::Sender<Message>,
    ready: Stop,
    stop: Stop,
    handler: Arc<PollHandler>,
    log: Log,
) {
    tokio::select! {
        () = ready.wait() => {}
        () = stop.wait() => return,
    }
    let mut last = std::time::Instant::now();
    loop {
        stop.sleep(SUMMARY_INTERVAL_S).await;
        if stop.is_set() {
            return;
        }
        let (reply, wait) = oneshot::channel();
        if core.send(Message::Summary { reply }).is_err() {
            return;
        }
        let Ok((mut phases, sizes)) = wait.await else {
            return;
        };
        let over_s = last.elapsed().as_secs_f64();
        last = std::time::Instant::now();
        let mut lines = Vec::new();
        for (name, stats) in handler.stats.lock().expect("stats").iter_mut() {
            lines.push(stats.line(name));
            stats.latencies_ms.clear();
        }
        let shell = std::mem::take(&mut *handler.phases.lock().expect("phases"));
        for (name, cpu) in &shell.cpu_s {
            phases.add(name, *cpu, shell.wall_s[name]);
        }
        lines.push(phases.line(over_s));
        lines.push(sizes);
        for line in lines {
            log(&line);
        }
    }
}

/// Rebase onto each newly activated release; reload the vehicle registry on each import.
async fn publications(
    config: &Config,
    core: &std::sync::mpsc::Sender<Message>,
    stop: &Stop,
    mut run_id: String,
    demand: Option<SharedDemand>,
    log: &Log,
) -> Result<(), HandlerError> {
    let (client, mut notes) = listen(config, PUBLICATION_CHANNEL).await?;
    client
        .batch_execute(&format!("LISTEN {REGISTRY_CHANNEL}"))
        .await?;
    loop {
        let note = tokio::select! {
            note = notes.recv() => note,
            () = stop.wait() => return Ok(()),
        };
        let Some(note) = note else {
            return Err("the listener connection closed".into());
        };
        if note == REGISTRY_CHANNEL {
            ask(core, |done| Message::ReloadRegistry { done }).await?;
            log("reloaded the vehicle registry");
            continue;
        }
        let current = active_loads(&client)
            .await
            .map_err(|e| -> HandlerError { e.to_string().into() })?;
        if current.run_id == run_id {
            continue;
        }
        let czptt = current.czptt;
        run_id = current.run_id.clone();
        ask(core, |done| Message::Switch {
            loads: current,
            done,
        })
        .await?;
        if let Some(demand) = &demand {
            demand.0.lock().await.switch(czptt);
        }
        log(&format!("rebased onto release {run_id}"));
    }
}
