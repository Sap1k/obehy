//! Warm replay: rebuild the worker's state from stored observations: port of `realtime/warm.py`
//! (docs/R1_SLICE.md section 5).
//!
//! The stored observations run through the same runner as live ones, with no writer, on the
//! replay clock: housekeeping ticks on round multiples of the emit tick, each before the first
//! observation at or after it, as in replay. Vehicles and journeys a live run forgets during the
//! window are forgotten here at the same time. Reading is `store::stored` (binary COPY on a
//! thread of its own, decoding there); the runner (R6) is what [`WarmRunner`] asks of it.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant as Monotonic;

use obehy_core::config::Policy;
use obehy_core::config::policy::WarmReplayMode;
use obehy_core::model::Observation;
use obehy_core::time::{Instant, TimeDelta, next_multiple};

use crate::store::StoreResult;
use crate::store::stored::Window;

pub const PROGRESS_EVERY_S: f64 = 30.0;

/// What warm replay asks of the runner: run a batch of observations (no writer attached), and
/// one housekeeping tick.
pub trait WarmRunner {
    fn process(&mut self, batch: Vec<Observation>);
    fn tick(&mut self, at: Instant);
}

/// A simulated clock's housekeeping ticks, as the worker's wall clock runs them: every
/// `every_s` on round multiples, each before the first observation after it (`runner.Ticks`).
#[derive(Debug, Clone)]
pub struct Ticks {
    pub every_s: i64,
    pub next: Option<Instant>,
}

impl Ticks {
    pub fn new(every_s: i64) -> Ticks {
        Ticks {
            every_s,
            next: None,
        }
    }

    pub fn due(&self, at: Instant) -> bool {
        self.next.is_some_and(|next| next <= at)
    }

    /// Run every tick up to and including `at`.
    pub fn advance(&mut self, at: Instant, runner: &mut dyn WarmRunner) {
        let mut next = self.next.unwrap_or_else(|| next_multiple(at, self.every_s));
        while next <= at {
            runner.tick(next);
            next = next_multiple(next, self.every_s);
        }
        self.next = Some(next);
    }

    /// The chunk in runs between ticks, each processed; the ticks between them run on the way.
    pub fn split(&mut self, chunk: Vec<Observation>, runner: &mut dyn WarmRunner) {
        let mut batch = Vec::new();
        for observation in chunk {
            if self.next.is_none() || self.due(observation.received_at) {
                if !batch.is_empty() {
                    runner.process(std::mem::take(&mut batch));
                }
                self.advance(observation.received_at, runner);
            }
            batch.push(observation);
        }
        if !batch.is_empty() {
            runner.process(batch);
        }
    }
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
/// runner, with ticks up to `until`; returns how many. Stops early when `stop` is set; a read
/// error ends it. `log` gets a progress line every [`PROGRESS_EVERY_S`] and at the end.
pub fn warm_start(
    runner: &mut dyn WarmRunner,
    chunks: impl IntoIterator<Item = StoreResult<Vec<Observation>>>,
    until: Instant,
    every_s: i64,
    stop: &AtomicBool,
    log: Option<&dyn Fn(&str)>,
) -> StoreResult<usize> {
    let mut ticks = Ticks::new(every_s);
    let mut count = 0;
    let started = Monotonic::now();
    let mut last = started;
    let mut reached: Option<Instant> = None;
    let mut chunks = chunks.into_iter();
    while !stop.load(Ordering::SeqCst) {
        let Some(chunk) = chunks.next() else {
            if count > 0 {
                ticks.advance(until, runner);
            }
            break;
        };
        let chunk = chunk?;
        count += chunk.len();
        reached = chunk.last().map(|o| o.received_at).or(reached);
        ticks.split(chunk, runner);
        if let Some(log) = log
            && last.elapsed().as_secs_f64() >= PROGRESS_EVERY_S
        {
            log(&progress(count, started.elapsed().as_secs_f64(), reached));
            last = Monotonic::now();
        }
    }
    if let Some(log) = log
        && count > 0
    {
        log(&progress(count, started.elapsed().as_secs_f64(), reached));
    }
    Ok(count)
}

fn progress(count: usize, elapsed_s: f64, reached: Option<Instant>) -> String {
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
    format!("warm replay: {count} observations in {elapsed_s:.0} s ({rate:.0}/s), at {at}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use obehy_core::model::{Feed, RawRef};

    #[derive(Default)]
    struct Seen(Vec<String>);

    impl WarmRunner for Seen {
        fn process(&mut self, batch: Vec<Observation>) {
            let items: Vec<String> = batch.iter().map(|o| o.raw.item.to_string()).collect();
            self.0.push(format!("batch {}", items.join(",")));
        }

        fn tick(&mut self, at: Instant) {
            self.0.push(format!("tick {}", at.micros() / 1_000_000));
        }
    }

    fn obs(seconds: i64, item: i64) -> Observation {
        Observation {
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
        }
    }

    #[test]
    fn ticks_run_before_the_first_observation_at_or_after_them() {
        let mut runner = Seen::default();
        let stop = AtomicBool::new(false);
        let chunks = vec![
            Ok(vec![obs(5, 0), obs(9, 1), obs(10, 2), obs(25, 3)]),
            Ok(vec![obs(31, 4)]),
        ];
        let count = warm_start(
            &mut runner,
            chunks,
            Instant::from_unix_seconds(45),
            10,
            &stop,
            None,
        )
        .unwrap();
        assert_eq!(count, 5);
        assert_eq!(
            runner.0,
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
        let policy = Policy::load(
            &std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../src/obehy/data/realtime")
                .join(obehy_core::config::policy::POLICY_FILE),
        )
        .unwrap();
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
