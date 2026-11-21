//! Warm replay: rebuild the worker's state from stored observations: port of `realtime/warm.py`
//! (docs/R1_SLICE.md section 5), and the `obehy-rt warm` command that times it.
//!
//! The stored observations run through the same runner as live ones, with no writer, on the
//! replay clock ([`Ticks::split`]): housekeeping ticks on round multiples of the emit tick, each
//! before the first observation at or after it, as in replay. Vehicles and journeys a live run
//! forgets during the window are forgotten here at the same time. Reading is `store::stored`
//! (binary COPY on a thread of its own, decoding there).
//!
//! `obehy-rt warm` warm-starts a runner over a release's serving files from `rt.observation`
//! (or, with `--from-archive`, from the archive's payloads decoded in place of the read) and
//! reports the time per phase and the resident memory; with `--then` it goes on through the
//! archive and writes that window's output stream (`compare --level output`), which is how warm
//! start is compared with Python's.

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::sync_channel;
use std::time::Instant as Monotonic;

use obehy_core::config::Policy;
use obehy_core::config::policy::WarmReplayMode;
use obehy_core::model::Observation;
use obehy_core::runner::{Loads, RunError, Runner, Ticks};
use obehy_core::time::{Instant, TimeDelta};

use crate::phases::Phases;
use crate::replay::{DecodeTimes, FileLoads, decoded_polls};
use crate::store::stored::{CHUNK, Window, spawn_reader};
use crate::store::{StoreError, StoreResult};

pub const PROGRESS_EVERY_S: f64 = 30.0;

/// Warm replay could not finish: the read failed, or the runner did.
#[derive(Debug, thiserror::Error)]
pub enum WarmError {
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Run(#[from] RunError),
}

/// What to read for a warm start ending at `until` (`warm_window`): the last `max_hours`, and
/// with `mode = "vehicles"` the reception time from which a vehicle still counts as live (seen
/// within `forget_after_s`, as `refresh` keeps it, plus the clock skew a source time may have).
pub fn warm_window(policy: &Policy, until: Instant) -> Window {
    let since = until - TimeDelta::from_seconds(policy.warm_replay.max_hours * 3600);
    let alive = match policy.warm_replay.mode {
        WarmReplayMode::Window => None,
        WarmReplayMode::Vehicles => {
            let alive_s = policy.lifecycle.forget_after_s + policy.time.max_clock_skew_s;
            Some(until - TimeDelta::from_seconds(alive_s))
        }
    };
    Window {
        since,
        until,
        alive,
    }
}

/// Run `chunks` (stored observations in `received_at` order, whole instants) through the
/// runner without side effects, with ticks up to `until`; returns how many. Stops early when
/// `stop` is set; a read error ends it. `log` gets a progress line every [`PROGRESS_EVERY_S`]
/// and at the end. The read is timed as the phase `read`.
pub fn warm_start<L: Loads>(
    runner: &mut Runner<L, Phases>,
    chunks: impl IntoIterator<Item = StoreResult<Vec<Arc<Observation>>>>,
    until: Instant,
    stop: &AtomicBool,
    log: Option<&dyn Fn(&str)>,
) -> Result<usize, WarmError> {
    let mut ticks = Ticks::new(&runner.policy);
    let mut count = 0;
    let started = Monotonic::now();
    let mut last = started;
    let mut reached: Option<Instant> = None;
    let mut chunks = chunks.into_iter();
    let discard = |_: &mut Runner<L, Phases>, _: Instant, _: Vec<obehy_core::model::Effect>| {
        Ok::<(), WarmError>(())
    };
    while !stop.load(Ordering::SeqCst) {
        let Some(chunk) = runner.phases.time("read", || chunks.next()) else {
            if count > 0 {
                ticks.advance(runner, until, discard)?;
            }
            break;
        };
        let chunk = chunk?;
        ticks.split(runner, &chunk, discard, |runner, batch| {
            runner.process(batch)?;
            Ok(())
        })?;
        count += chunk.len();
        reached = chunk.last().map(|o| o.received_at).or(reached);
        if let Some(log) = log
            && last.elapsed().as_secs_f64() >= PROGRESS_EVERY_S
        {
            log(&progress(
                count,
                started.elapsed().as_secs_f64(),
                reached,
                &runner.phases,
            ));
            last = Monotonic::now();
        }
    }
    if let Some(log) = log
        && count > 0
    {
        log(&progress(
            count,
            started.elapsed().as_secs_f64(),
            reached,
            &runner.phases,
        ));
    }
    Ok(count)
}

fn progress(count: usize, elapsed_s: f64, reached: Option<Instant>, phases: &Phases) -> String {
    let at = reached.map_or_else(
        || "-".to_owned(),
        |at| {
            let text = at.isoformat();
            // `isoformat(timespec="seconds")`: no fraction.
            match text.find('.') {
                Some(dot) => format!("{}+00:00", &text[..dot]),
                None => text,
            }
        },
    );
    let rate = if elapsed_s > 0.0 {
        count as f64 / elapsed_s
    } else {
        0.0
    };
    format!(
        "warm replay: {count} observations in {elapsed_s:.0} s ({rate:.0}/s), at {at}; {}",
        phases.line(elapsed_s.max(1e-9))
    )
}

/// Stored observations of `window` from the database at `url`, as warm start reads them.
pub fn stored_chunks(
    url: &str,
    window: Window,
) -> impl Iterator<Item = StoreResult<Vec<Arc<Observation>>>> {
    spawn_reader(url, window, CHUNK)
        .map(|chunk| chunk.map(|observations| observations.into_iter().map(Arc::new).collect()))
}

/// The archive window's decoded polls as warm start's chunks (one per poll with observations),
/// decoding standing in for reading `rt.observation`: decoded on a thread of its own (and
/// `threads` decoders), handed over through a short queue.
pub fn archive_chunks<R>(
    setup: &crate::compare::Setup,
    threads: usize,
    times: &DecodeTimes,
    body: impl FnOnce(&mut dyn Iterator<Item = StoreResult<Vec<Arc<Observation>>>>) -> R,
) -> R {
    let skew = setup.policy.time.max_clock_skew();
    let (sender, receiver) = sync_channel::<StoreResult<Vec<Arc<Observation>>>>(64);
    std::thread::scope(|scope| {
        scope.spawn(move || {
            let outcome = decoded_polls(
                &setup.window,
                &setup.decoders,
                skew,
                threads,
                |poll| {
                    if let Some(observations) = poll.observations
                        && !observations.is_empty()
                    {
                        sender.send(Ok(observations)).map_err(|_| {
                            crate::replay::ReplayError::Other("warm start stopped".into())
                        })?;
                    }
                    Ok(())
                },
                times,
            );
            if let Err(error) = outcome {
                let _ = sender.send(Err(StoreError::other(error.to_string())));
            }
        });
        let mut chunks = receiver.into_iter();
        let result = body(&mut chunks);
        drop(chunks); // a decoder blocked on the queue stops
        result
    })
}

// --- the warm command ------------------------------------------------------------------------

#[derive(Debug, Clone, clap::Args)]
pub struct WarmArgs {
    /// Release directory: the index from its serving files.
    #[arg(long = "from-files")]
    pub release: PathBuf,
    /// Warm start up to this Prague wall time (`YYYY-MM-DDTHH:MM`; an explicit offset is kept).
    #[arg(long)]
    pub until: String,
    /// Read the last this many hours (default: policy `warm_replay.max_hours`).
    #[arg(long)]
    pub hours: Option<i64>,
    /// `window` or `vehicles` (default: policy `warm_replay.mode`).
    #[arg(long)]
    pub mode: Option<String>,
    /// The database URL from this environment variable (default: `[database] url`).
    #[arg(long = "url-env")]
    pub url_env: Option<String>,
    /// Decode the archive window instead of reading `rt.observation` (`--hours` back from
    /// `--until`, every observation: the mode is ignored).
    #[arg(long = "from-archive")]
    pub from_archive: bool,
    /// The archive (`--from-archive`, `--then`).
    #[arg(long, default_value = "data/rt-raw")]
    pub archive: PathBuf,
    /// Comma-separated source IDs (`--from-archive`, `--then`).
    #[arg(long)]
    pub sources: Option<String>,
    #[arg(long, default_value = "jdf,czptt")]
    pub feeds: String,
    /// Then go on through the archive up to this local time, writing the output stream.
    #[arg(long)]
    pub then: Option<String>,
    /// The output stream of `--then` (JSON lines; `.gz`: gzip).
    #[arg(long)]
    pub out: Option<PathBuf>,
    /// State tables and GTFS-RT this often in the `--then` stream (0: only at the end).
    #[arg(long = "gtfs-rt-every", default_value_t = 60)]
    pub gtfs_rt_every: i64,
    /// Threads decoding payloads (1: inline).
    #[arg(long, default_value_t = 4)]
    pub threads: usize,
}

fn window_args(args: &WarmArgs, since: Instant, until: Instant) -> crate::compare::WindowArgs {
    let day = |at: Instant| at.utc().date().isoformat();
    crate::compare::WindowArgs {
        release: Some(args.release.clone()),
        start: day(since),
        end: day(until),
        archive: args.archive.clone(),
        sources: args.sources.clone(),
        feeds: args.feeds.clone(),
        manifest: None,
        since: Some(since.isoformat()),
        until: Some(until.isoformat()),
        all_feeds: false,
        threads: args.threads,
    }
}

fn megabytes(bytes: Option<u64>) -> String {
    bytes.map_or_else(
        || "?".to_owned(),
        |b| format!("{:.0} MB", b as f64 / 1_048_576.0),
    )
}

/// The process's resident memory now, in bytes (working set on Windows, RSS on Linux).
pub fn rss_bytes() -> Option<u64> {
    memory_stats::memory_stats().map(|m| m.physical_mem as u64)
}

/// `obehy-rt warm`.
pub fn run_command(args: &WarmArgs) -> ExitCode {
    match run(args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &WarmArgs) -> Result<(), String> {
    let text = |e: &dyn std::fmt::Display| e.to_string();
    let paths = crate::settings::Paths::discover().map_err(|e| text(&e))?;
    let mut policy = paths.load_policy().map_err(|e| text(&e))?;
    if let Some(hours) = args.hours {
        policy.warm_replay.max_hours = hours;
    }
    match args.mode.as_deref() {
        None => {}
        Some("window") => policy.warm_replay.mode = WarmReplayMode::Window,
        Some("vehicles") => policy.warm_replay.mode = WarmReplayMode::Vehicles,
        Some(other) => return Err(format!("--mode is window or vehicles, not {other:?}")),
    }
    let until = crate::compare::local_time(&args.until)?;
    let window = warm_window(&policy, until);
    let feeds = crate::compare::parse_feeds(&args.feeds)?;
    // The archive's channels: what `--from-archive` decodes, `--then` replays, and the core
    // semantics of every source named.
    let setup = crate::compare::setup(&window_args(args, window.since, until))?;
    let semantics = crate::output::semantics_of(&setup.window);
    let policy = Arc::new(policy);
    let started = Monotonic::now();
    let loads = FileLoads::open(&args.release).map_err(|e| text(&e))?;
    let mut runner = Runner::new(loads, policy.clone(), &feeds, semantics, Phases::new())
        .map_err(|e| text(&e))?;
    let rss_before = rss_bytes();
    let peak = crate::bench::PeakRss::start();
    let stop = AtomicBool::new(false);
    let log = |line: &str| eprintln!("{line}");
    let times = DecodeTimes::default();
    let count = if args.from_archive {
        archive_chunks(&setup, args.threads, &times, |chunks| {
            warm_start(&mut runner, chunks, until, &stop, Some(&log))
        })
    } else {
        let url = match &args.url_env {
            Some(name) => std::env::var(name).map_err(|e| format!("{name}: {e}"))?,
            None => {
                crate::settings::load_database_url(&paths.config(), &crate::settings::ProcessEnv)
                    .map_err(|e| text(&e))?
            }
        };
        warm_start(
            &mut runner,
            stored_chunks(&url, window),
            until,
            &stop,
            Some(&log),
        )
    }
    .map_err(|e| text(&e))?;
    let elapsed = started.elapsed().as_secs_f64();
    let phases: Vec<String> = runner
        .phases
        .cpu_s
        .iter()
        .map(|(name, cpu)| format!("{name} {cpu:.2}"))
        .collect();
    println!(
        "warm start: {count} observations ({} .. {}{}) in {elapsed:.1} s ({:.0}/s), ready",
        window.since,
        window.until,
        if args.from_archive {
            ", from the archive".to_owned()
        } else {
            window
                .alive
                .map_or_else(String::new, |a| format!(", vehicles alive from {a}"))
        },
        count as f64 / elapsed.max(1e-9)
    );
    println!(
        "cpu s: {}{}; rss {} before, {} after, peak {}",
        phases.join(", "),
        if args.from_archive {
            format!(", decode {:.2} (all threads)", times.seconds())
        } else {
            String::new()
        },
        megabytes(rss_before),
        megabytes(rss_bytes()),
        megabytes(peak.peak()),
    );
    let Some(then) = &args.then else {
        return Ok(());
    };
    let out = args.out.as_ref().ok_or("--then needs --out")?;
    let then = crate::compare::local_time(then)?;
    let next = crate::compare::setup(&window_args(args, until, then))?;
    let options = crate::output::Options {
        release: args.release.clone(),
        feeds,
        gtfs_rt_every_s: Some(args.gtfs_rt_every).filter(|e| *e != 0),
        threads: args.threads,
        diagnostics: false,
        quality: false,
    };
    runner.phases.reset();
    let written = crate::compare::write_stream(out, |emit| {
        crate::output::output_from(
            runner,
            &next.window,
            &options,
            &next.decoders,
            &next.sources,
            emit,
        )
    })
    .map_err(|e| text(&e))?;
    println!("{written} records in {}", out.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use obehy_core::model::{Feed, RawRef};
    use obehy_core::runner::Untimed;

    fn obs(seconds: i64, item: i64) -> Arc<Observation> {
        Arc::new(Observation {
            source: "s".into(),
            channel: "c".into(),
            feed: Feed::Jdf,
            received_at: Instant::from_unix_seconds(seconds),
            observed_at: None,
            raw: RawRef {
                sha256: "x".into(),
                item,
            },
            decoder_version: 1,
            facts: Vec::new(),
        })
    }

    fn policy() -> Policy {
        Policy::load(
            &std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../src/obehy/data/realtime")
                .join(obehy_core::config::policy::POLICY_FILE),
        )
        .unwrap()
    }

    #[test]
    fn ticks_run_before_the_first_observation_at_or_after_them() {
        let mut policy = policy();
        policy.emit.tick_s = 10;
        let loads = FileLoads {
            release: PathBuf::new(),
            run_id: "run".into(),
        };
        // No feeds: the ticks and runs are what is seen.
        let mut runner =
            Runner::new(loads, Arc::new(policy), &[], Default::default(), Untimed).unwrap();
        let mut ticks = Ticks::new(&runner.policy);
        let mut seen: Vec<String> = Vec::new();
        let chunks = [
            vec![obs(5, 0), obs(9, 1), obs(10, 2), obs(25, 3)],
            vec![obs(31, 4)],
        ];
        for chunk in &chunks {
            let seen = std::cell::RefCell::new(&mut seen);
            ticks
                .split(
                    &mut runner,
                    chunk,
                    |_, at, _| {
                        seen.borrow_mut()
                            .push(format!("tick {}", at.micros() / 1_000_000));
                        Ok::<(), RunError>(())
                    },
                    |_, batch| {
                        let items: Vec<String> =
                            batch.iter().map(|o| o.raw.item.to_string()).collect();
                        seen.borrow_mut().push(format!("batch {}", items.join(",")));
                        Ok(())
                    },
                )
                .unwrap();
        }
        ticks
            .advance(&mut runner, Instant::from_unix_seconds(45), |_, at, _| {
                seen.push(format!("tick {}", at.micros() / 1_000_000));
                Ok::<(), RunError>(())
            })
            .unwrap();
        assert_eq!(
            seen,
            [
                "batch 0,1",
                "tick 10",
                "batch 2",
                "tick 20",
                "batch 3",
                "tick 30",
                "batch 4",
                "tick 40",
            ]
        );
    }

    #[test]
    fn the_window_follows_the_policy() {
        let policy = policy();
        let until = Instant::from_unix_seconds(100_000);
        let window = warm_window(&policy, until);
        assert_eq!(
            (until - window.since).total_seconds(),
            (policy.warm_replay.max_hours * 3600) as f64
        );
        match policy.warm_replay.mode {
            WarmReplayMode::Window => assert_eq!(window.alive, None),
            WarmReplayMode::Vehicles => assert_eq!(
                window.alive,
                Some(
                    until
                        - TimeDelta::from_seconds(
                            policy.lifecycle.forget_after_s + policy.time.max_clock_skew_s
                        )
                )
            ),
        }
    }
}
