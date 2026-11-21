//! `obehy-rt replay`: archived payloads through the runner with a simulated clock. Port of the
//! replay loop of `realtime/replay.py` (`decoded_polls`, `drive`, `Clock`, `finish`), which
//! `obehy-rt compare --level output` and the replay command share.
//!
//! Polls of the selected channels are merged in reception order ([`crate::compare::merged_polls`]),
//! decoded by their connector, and run through the same [`Runner`] as the worker, a chunk of
//! [`LOOKAHEAD_POLLS`] at a time (the chunk's keys are loaded first). Housekeeping ticks fall on
//! fixed multiples of the emit interval, so output never depends on when the replay runs.
//!
//! Decoding runs ahead of the core on a pool of threads ([`decoded_polls`]); polls come out in
//! archive order whatever the pool does, so it cannot change the output.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc;

use obehy_core::config::Channel;
use obehy_core::index::RowsError;
use obehy_core::model::{Effect, Feed, Observation};
use obehy_core::runner::{Loads, Phases, RunError, Runner, Ticks};
use obehy_core::time::{Instant, TimeDelta, next_multiple};
use obehy_sources::reader::json::Json;
use obehy_sources::{Decoders, PayloadError};
use serde_json::{Map, Value};

use crate::compare::{Window, json_of, merged_polls};
use crate::index::{ServingRows, release_run_id, serving_dir};

impl Phases for crate::phases::Phases {
    fn time<T>(&mut self, name: &str, f: impl FnOnce() -> T) -> T {
        crate::phases::Phases::time(self, name, f)
    }

    fn count(&mut self, name: &str, n: u64) {
        crate::phases::Phases::count(self, name, n);
    }
}

/// Polls whose keys are loaded together before they are stepped.
pub const LOOKAHEAD_POLLS: usize = 40;

#[derive(Debug, thiserror::Error)]
pub enum ReplayError {
    #[error(transparent)]
    Run(#[from] RunError),
    #[error(transparent)]
    Io(#[from] io::Error),
    #[error("{0}")]
    Other(String),
}

impl From<RowsError> for ReplayError {
    fn from(error: RowsError) -> ReplayError {
        ReplayError::Run(RunError::Rows(error))
    }
}

/// A release directory's serving files (`index_sql.file_loads`).
#[derive(Debug, Clone)]
pub struct FileLoads {
    pub release: PathBuf,
    pub run_id: String,
}

impl FileLoads {
    pub fn open(release: &Path) -> Result<FileLoads, RowsError> {
        Ok(FileLoads {
            release: release.to_path_buf(),
            run_id: release_run_id(release)?,
        })
    }
}

impl Loads for FileLoads {
    type Rows = ServingRows;

    fn release_id(&self) -> &str {
        &self.run_id
    }

    fn rows(&self, feed: Feed) -> Result<ServingRows, RowsError> {
        ServingRows::open(&serving_dir(&self.release, feed))
    }
}

/// One archived poll of the window and its observations; None for a failed poll or, with the
/// decoder's error (class, message), an undecodable payload.
#[derive(Debug, Clone)]
pub struct DecodedPoll {
    pub at: Instant,
    pub source: String,
    pub channel: String,
    pub entry: Map<String, Value>,
    pub observations: Option<Vec<Arc<Observation>>>,
    pub error: Option<(String, String)>,
    /// The channel's decoder version.
    pub decoder_version: i64,
}

/// The CPU time spent reading and decoding payloads, over every decoding thread.
#[derive(Debug, Default)]
pub struct DecodeTimes {
    nanos: std::sync::atomic::AtomicU64,
}

impl DecodeTimes {
    fn add(&self, spent: std::time::Duration) {
        let nanos = u64::try_from(spent.as_nanos()).unwrap_or(u64::MAX);
        self.nanos
            .fetch_add(nanos, std::sync::atomic::Ordering::Relaxed);
    }

    pub fn seconds(&self) -> f64 {
        self.nanos.load(std::sync::atomic::Ordering::Relaxed) as f64 / 1e9
    }
}

/// A poll read from the archive, not decoded yet.
struct Pending {
    at: Instant,
    source: String,
    channel: String,
    poll: crate::archive::ArchivedPoll,
}

fn decode(
    pending: Pending,
    window: &Window,
    decoders: &Decoders,
    skew: TimeDelta,
    times: &DecodeTimes,
) -> io::Result<DecodedPoll> {
    let started = cpu_time::ThreadTime::try_now().ok();
    let decoded = decode_poll(pending, window, decoders, skew);
    if let Some(started) = started {
        times.add(started.elapsed());
    }
    decoded
}

fn decode_poll(
    pending: Pending,
    window: &Window,
    decoders: &Decoders,
    skew: TimeDelta,
) -> io::Result<DecodedPoll> {
    let Pending {
        at,
        source,
        channel,
        poll,
    } = pending;
    let body = poll.body().map_err(|e| io::Error::other(e.to_string()))?;
    let mut decoded = DecodedPoll {
        at,
        source,
        channel,
        entry: Map::new(),
        observations: None,
        error: None,
        decoder_version: 0,
    };
    decoded.decoder_version = decoders
        .version(&decoded.source, &decoded.channel)
        .ok_or_else(|| {
            io::Error::other(format!(
                "no realtime connector decodes {}/{}",
                decoded.source, decoded.channel
            ))
        })?;
    if let (Some(body), true) = (body, poll.succeeded()) {
        let sha256 = poll
            .entry
            .get("sha256")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let request = poll.entry.get("request").map(json_of);
        let channel = window
            .channels
            .iter()
            .find(|c| c.source == decoded.source && c.channel == decoded.channel)
            .expect("a selected channel");
        let payload = Payload {
            body: &body,
            sha256,
            received_at: at,
            request: request.as_ref(),
        };
        match decode_payload(decoders, channel, &payload, skew, window.all_feeds) {
            Err(error) => decoded.error = Some((error.class.to_owned(), error.message)),
            Ok(observations) => decoded.observations = Some(observations),
        }
    }
    decoded.entry = poll.entry;
    Ok(decoded)
}

/// A stored payload as a decoder reads it: the stored body (filtered if the channel says so),
/// its sha256, the reception time and the poll's archived request (a board's station).
#[derive(Debug, Clone, Copy)]
pub struct Payload<'a> {
    pub body: &'a [u8],
    pub sha256: &'a str,
    pub received_at: Instant,
    pub request: Option<&'a Json>,
}

/// A facts channel's payload decoded, only the observations of the channel's live feeds unless
/// `all_feeds`: the one decoding step replay and the live worker share (`decoded_polls`,
/// `worker.PollHandler.decode`).
pub fn decode_payload(
    decoders: &Decoders,
    channel: &Channel,
    payload: &Payload<'_>,
    skew: TimeDelta,
    all_feeds: bool,
) -> Result<Vec<Arc<Observation>>, PayloadError> {
    let observations = decoders.decode(
        &channel.source,
        &channel.channel,
        payload.body,
        payload.sha256,
        payload.received_at,
        skew,
        payload.request,
    )?;
    Ok(observations
        .into_iter()
        .filter(|o| all_feeds || channel.is_live(o.feed.as_str()))
        .map(Arc::new)
        .collect())
}

/// Each poll of the window (`since`..`until`) in reception order, decoded (only the
/// observations of live feeds unless `all_feeds`), handed to `each` in order. Payloads are
/// decoded on `threads` threads (1: inline).
pub fn decoded_polls(
    window: &Window,
    decoders: &Decoders,
    skew: TimeDelta,
    threads: usize,
    mut each: impl FnMut(DecodedPoll) -> Result<(), ReplayError>,
    times: &DecodeTimes,
) -> Result<(), ReplayError> {
    let selected = merged_polls(window).filter_map(|found| match found {
        Err(e) => Some(Err(e)),
        Ok((at, ..)) if window.since.is_some_and(|s| at < s) => None,
        Ok((at, source, channel, poll)) => Some(Ok(Pending {
            at,
            source,
            channel,
            poll,
        })),
    });
    let until = window.until;
    if threads <= 1 {
        for pending in selected {
            let pending = pending?;
            if until.is_some_and(|u| pending.at >= u) {
                return Ok(());
            }
            each(decode(pending, window, decoders, skew, times)?)?;
        }
        return Ok(());
    }
    // A bounded window of polls in flight: each is decoded by whichever worker takes it and
    // put back in its place by sequence number.
    std::thread::scope(|scope| -> Result<(), ReplayError> {
        let (work_tx, work_rx) = mpsc::sync_channel::<(usize, Pending)>(threads * 4);
        let work_rx = Arc::new(std::sync::Mutex::new(work_rx));
        let (done_tx, done_rx) =
            mpsc::sync_channel::<(usize, io::Result<DecodedPoll>)>(threads * 8);
        for _ in 0..threads {
            let work_rx = Arc::clone(&work_rx);
            let done_tx = done_tx.clone();
            scope.spawn(move || {
                loop {
                    let next = work_rx.lock().expect("the work queue").recv();
                    let Ok((n, pending)) = next else {
                        return;
                    };
                    if done_tx
                        .send((n, decode(pending, window, decoders, skew, times)))
                        .is_err()
                    {
                        return;
                    }
                }
            });
        }
        drop(done_tx);
        let mut waiting: std::collections::BTreeMap<usize, io::Result<DecodedPoll>> =
            std::collections::BTreeMap::new();
        let (mut sent, mut next) = (0_usize, 0_usize);
        let mut source = selected;
        let mut exhausted = false;
        let mut outcome: Result<(), ReplayError> = Ok(());
        'polls: loop {
            while !exhausted && sent - next < threads * 4 {
                match source.next() {
                    None => exhausted = true,
                    Some(Err(e)) => {
                        outcome = Err(e.into());
                        break 'polls;
                    }
                    Some(Ok(pending)) => {
                        if until.is_some_and(|u| pending.at >= u) {
                            exhausted = true;
                        } else {
                            work_tx.send((sent, pending)).expect("a decoder thread");
                            sent += 1;
                        }
                    }
                }
            }
            if next == sent {
                break;
            }
            while !waiting.contains_key(&next) {
                let (n, decoded) = done_rx.recv().expect("a decoder thread");
                waiting.insert(n, decoded);
            }
            let decoded = waiting.remove(&next).expect("the next poll");
            next += 1;
            if let Err(e) = decoded.map_err(ReplayError::from).and_then(&mut each) {
                outcome = Err(e);
                break;
            }
        }
        drop(work_tx);
        outcome
    })
}

/// What the replay loop reports to: each step's effects, each tick's, and the snapshots.
pub trait Sink<L: Loads, P: Phases> {
    /// A poll and its effects (None: failed or undecodable).
    fn stepped(
        &mut self,
        runner: &mut Runner<L, P>,
        poll: &DecodedPoll,
        effects: Option<Vec<Effect>>,
    ) -> Result<(), ReplayError>;
    /// A tick's effects, after it ran.
    fn ticked(
        &mut self,
        runner: &mut Runner<L, P>,
        at: Instant,
        effects: Vec<Effect>,
    ) -> Result<(), ReplayError>;
    /// A snapshot is due at `at` (every `gtfs_rt_every_s`, on round multiples).
    fn snapshot(&mut self, runner: &mut Runner<L, P>, at: Instant) -> Result<(), ReplayError>;
    /// After each tick's effects, before its predictor sample and snapshot (journey samples).
    fn sampled(&mut self, _runner: &mut Runner<L, P>, _at: Instant) -> Result<(), ReplayError> {
        Ok(())
    }
    /// A predictor sample is due at `at` (with `Clock::sample_every_s`).
    fn predictors(&mut self, _runner: &mut Runner<L, P>, _at: Instant) -> Result<(), ReplayError> {
        Ok(())
    }
}

/// The simulated clock: the runner's ticks, and with them a snapshot every `every_s` on round
/// multiples (at most one per tick, as Python's `Clock`).
#[derive(Debug, Clone)]
pub struct Clock {
    pub ticks: Ticks,
    pub every_s: Option<i64>,
    pub next_snapshot: Option<Instant>,
    /// Predictor samples this often (on round multiples), at the first tick at or after each.
    pub sample_every_s: Option<i64>,
    pub next_sample: Option<Instant>,
}

impl Clock {
    pub fn new(ticks: Ticks, every_s: Option<i64>) -> Clock {
        Clock {
            ticks,
            every_s: every_s.filter(|e| *e > 0),
            next_snapshot: None,
            sample_every_s: None,
            next_sample: None,
        }
    }

    pub fn advance<L: Loads, P: Phases>(
        &mut self,
        runner: &mut Runner<L, P>,
        at: Instant,
        sink: &mut impl Sink<L, P>,
    ) -> Result<(), ReplayError> {
        if self.ticks.next.is_none() {
            if let Some(every) = self.every_s {
                self.next_snapshot = Some(next_multiple(at, every));
            }
            if let Some(every) = self.sample_every_s {
                self.next_sample = Some(next_multiple(at, every));
            }
        }
        let every = self.every_s;
        let sample_every = self.sample_every_s.unwrap_or(0);
        let next_snapshot = &mut self.next_snapshot;
        let next_sample = &mut self.next_sample;
        self.ticks.advance(runner, at, |runner, tick, effects| {
            sink.ticked(runner, tick, effects)?;
            sink.sampled(runner, tick)?;
            if let Some(due) = *next_sample
                && due <= tick
            {
                sink.predictors(runner, due)?;
                *next_sample = Some(due + TimeDelta::from_seconds(sample_every));
            }
            if let Some(due) = *next_snapshot
                && due <= tick
            {
                sink.snapshot(runner, due)?;
                *next_snapshot = Some(due + TimeDelta::from_seconds(every.unwrap_or(0)));
            }
            Ok::<(), ReplayError>(())
        })
    }
}

/// Run the decoded polls through the runner on the simulated clock; the sink sees each poll
/// and its effects. The last decoded poll's time, if any ran.
pub fn drive<L: Loads, P: Phases>(
    runner: &mut Runner<L, P>,
    polls: impl FnOnce(
        &mut dyn FnMut(DecodedPoll) -> Result<(), ReplayError>,
    ) -> Result<(), ReplayError>,
    clock: &mut Clock,
    sink: &mut impl Sink<L, P>,
) -> Result<Option<Instant>, ReplayError> {
    let mut last: Option<Instant> = None;
    let mut chunk: Vec<DecodedPoll> = Vec::with_capacity(LOOKAHEAD_POLLS);
    let mut run =
        |chunk: &mut Vec<DecodedPoll>, last: &mut Option<Instant>| -> Result<(), ReplayError> {
            // Load the static data of the next polls' keys in one go.
            let observations: Vec<Arc<Observation>> = chunk
                .iter()
                .flat_map(|p| p.observations.iter().flatten().cloned())
                .collect();
            runner.prefetch(&observations)?;
            for poll in chunk.drain(..) {
                clock.advance(runner, poll.at, sink)?;
                match &poll.observations {
                    None => sink.stepped(runner, &poll, None)?,
                    Some(observations) => {
                        let effects = runner.process(observations)?;
                        sink.stepped(runner, &poll, Some(effects))?;
                        *last = Some(poll.at);
                    }
                }
            }
            Ok(())
        };
    polls(&mut |poll| {
        chunk.push(poll);
        if chunk.len() == LOOKAHEAD_POLLS {
            run(&mut chunk, &mut last)?;
        }
        Ok(())
    })?;
    run(&mut chunk, &mut last)?;
    Ok(last)
}

/// Final housekeeping and estimates at the last poll: the final tick's effects.
pub fn finish<L: Loads, P: Phases>(
    runner: &mut Runner<L, P>,
    last: Instant,
) -> Result<Vec<Effect>, ReplayError> {
    Ok(runner.tick(last)?)
}

// --- the replay command ----------------------------------------------------------------------

#[derive(Debug, Clone, clap::Args)]
pub struct ReplayArgs {
    #[command(flatten)]
    pub window: crate::compare::WindowArgs,
    /// Where GTFS-RT snapshots go (`gtfs-rt/<feed>/<UTC time>.pb`).
    #[arg(long, default_value = "data/replay")]
    pub out: PathBuf,
    /// Write a GTFS-RT snapshot of every feed this often (simulated seconds).
    #[arg(long = "gtfs-rt-every")]
    pub gtfs_rt_every: Option<i64>,
    /// Rebuild `rt.observation` and history of the replayed days in the database
    /// (`crate::history`).
    #[arg(long = "write-history")]
    pub write_history: bool,
    /// With --write-history: the database URL from this environment variable (default:
    /// `[database] url`).
    #[arg(long = "url-env")]
    pub url_env: Option<String>,
}

/// Counts what the replay produced and writes the GTFS-RT snapshots.
struct Replayed {
    out: PathBuf,
    lifecycle: obehy_core::config::policy::LifecyclePolicy,
    polls: u64,
    failed: u64,
    effects: std::collections::BTreeMap<&'static str, u64>,
    snapshots: u64,
}

fn effect_name(effect: &Effect) -> &'static str {
    match effect {
        Effect::SnapshotJourney(_) => "SnapshotJourney",
        Effect::WriteEvent(_) => "WriteEvent",
        Effect::AssignVehicle(_) => "AssignVehicle",
        Effect::ObservationResult(_) => "ObservationResult",
        Effect::LinkJourneys(_) => "LinkJourneys",
        Effect::RecordPlatform(_) => "RecordPlatform",
        Effect::RecordCancellation(_) => "RecordCancellation",
        Effect::Unresolved(_) => "Unresolved",
    }
}

impl Replayed {
    fn count(&mut self, effects: &[Effect]) {
        for effect in effects {
            *self.effects.entry(effect_name(effect)).or_default() += 1;
        }
    }
}

impl Sink<FileLoads, crate::phases::Phases> for Replayed {
    fn stepped(
        &mut self,
        _runner: &mut Runner<FileLoads, crate::phases::Phases>,
        poll: &DecodedPoll,
        effects: Option<Vec<Effect>>,
    ) -> Result<(), ReplayError> {
        self.polls += 1;
        match effects {
            Some(effects) => self.count(&effects),
            None => self.failed += u64::from(poll.observations.is_none()),
        }
        Ok(())
    }

    fn ticked(
        &mut self,
        _runner: &mut Runner<FileLoads, crate::phases::Phases>,
        _at: Instant,
        effects: Vec<Effect>,
    ) -> Result<(), ReplayError> {
        self.count(&effects);
        Ok(())
    }

    fn snapshot(
        &mut self,
        runner: &mut Runner<FileLoads, crate::phases::Phases>,
        at: Instant,
    ) -> Result<(), ReplayError> {
        runner.estimate(at)?;
        let name: String = crate::canonical::utc_text(at)
            .chars()
            .filter(|c| *c != '-' && *c != ':')
            .collect();
        for feed in runner.sorted_feeds() {
            let state = &runner.runtimes[&feed].state;
            let bytes = runner.phases.time("gtfs_rt", || {
                obehy_core::publish::gtfs_rt::feed_bytes(state, at, &self.lifecycle)
            });
            let dir = self.out.join("gtfs-rt").join(feed.as_str());
            std::fs::create_dir_all(&dir)?;
            let path = dir.join(format!("{name}.pb"));
            let temporary = dir.join(format!(".partial-{name}.pb"));
            std::fs::write(&temporary, bytes)?;
            std::fs::rename(&temporary, &path)?;
        }
        self.snapshots += 1;
        Ok(())
    }
}

/// `obehy-rt replay --from-files`: the window through the runner, GTFS-RT snapshots if asked,
/// and a summary with the time per phase.
pub fn run_command(args: &ReplayArgs) -> std::process::ExitCode {
    if args.write_history {
        return crate::history::run_command(args);
    }
    let fail = |message: String| {
        eprintln!("error: {message}");
        std::process::ExitCode::FAILURE
    };
    let Some(release) = &args.window.release else {
        return fail("replay needs --from-files <release dir> (no database replay yet)".into());
    };
    let setup = match crate::compare::setup(&args.window) {
        Ok(s) => s,
        Err(e) => return fail(e),
    };
    let feeds = match crate::compare::parse_feeds(&args.window.feeds) {
        Ok(f) => f,
        Err(e) => return fail(e),
    };
    let loads = match FileLoads::open(release) {
        Ok(l) => l,
        Err(e) => return fail(e.to_string()),
    };
    let skew = setup.policy.time.max_clock_skew();
    let policy = Arc::new(setup.policy);
    let semantics = crate::output::semantics_of(&setup.window);
    let mut runner = match Runner::new(
        loads,
        policy.clone(),
        &feeds,
        semantics,
        crate::phases::Phases::new(),
    ) {
        Ok(r) => r,
        Err(e) => return fail(e.to_string()),
    };
    runner.estimate_threads = args.window.threads.max(1);
    let mut sink = Replayed {
        out: args.out.clone(),
        lifecycle: policy.lifecycle.clone(),
        polls: 0,
        failed: 0,
        effects: std::collections::BTreeMap::new(),
        snapshots: 0,
    };
    let mut clock = Clock::new(Ticks::new(&policy), args.gtfs_rt_every);
    let times = DecodeTimes::default();
    let started = std::time::Instant::now();
    let outcome = drive(
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
    .and_then(|last| match last {
        Some(last) => finish(&mut runner, last).map(|e| sink.count(&e)),
        None => Ok(()),
    });
    if let Err(e) = outcome {
        return fail(e.to_string());
    }
    let wall = started.elapsed().as_secs_f64();
    let observations = runner
        .phases
        .counts
        .get("observations")
        .copied()
        .unwrap_or(0);
    println!(
        "{} polls ({} failed), {observations} observations, {} snapshots in {wall:.1} s ({:.0} observations/s)",
        sink.polls,
        sink.failed,
        sink.snapshots,
        observations as f64 / wall.max(1e-9)
    );
    let effects: Vec<String> = sink
        .effects
        .iter()
        .map(|(k, v)| format!("{k} {v}"))
        .collect();
    println!("effects: {}", effects.join(", "));
    let phases: Vec<String> = runner
        .phases
        .cpu_s
        .iter()
        .map(|(name, cpu)| format!("{name} {cpu:.2}"))
        .collect();
    println!(
        "cpu s: {}, decode {:.2} (all threads); skipped {}",
        phases.join(", "),
        times.seconds(),
        runner.skipped
    );
    std::process::ExitCode::SUCCESS
}
