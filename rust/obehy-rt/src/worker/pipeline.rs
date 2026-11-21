//! What the worker does after the core: the shell half of Python's `Runner` (its `writer`,
//! `emitters`, `alerts` and `gtfs_rt_dir`) around the core's [`Runner`].
//!
//! [`Output`] writes a step's or tick's effects and, on an emitting tick, the current-state
//! tables and each feed's GTFS-RT file (Python's `Runner.run` and `Runner.tick(emit=True)`).
//! [`Pipeline`] is the live order: every poll first runs the housekeeping ticks due before it
//! (round multiples of `emit.tick_s`, [`Ticks`]), then its observations through the runner, its
//! effects to the writer. Replay drives the same runner and the same [`Output`] through
//! `replay::drive` (a [`ReplaySink`]), so live and replay run one path: decoding is
//! `replay::decode_payload`, the core the runner, writing this.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;

use obehy_core::config::policy::LifecyclePolicy;
use obehy_core::model::{Effect, Feed, FeedState, Observation};
use obehy_core::publish::alerts::{AlertEntities, AlertPeriod, AlertRow, AlertTarget};
use obehy_core::publish::gtfs_rt::{FeedEmitter, source_form};
use obehy_core::runner::{Loads, Runner, Ticks};
use obehy_core::time::Instant;
use obehy_core::vehicles::VehicleBook;
use serde_json::Value;

use super::WorkerError;
use crate::archive::write_atomic;
use crate::phases::Phases;
use crate::replay::{DecodedPoll, ReplayError, Sink};
use crate::store::alerts::{AlertFeedRow, alert_feed_rows};
use crate::store::{PgStore, Writer};

/// The worker's writer: PostgreSQL and the vehicle book.
pub type LiveWriter = Writer<PgStore, VehicleBook>;

/// Where effects, state and GTFS-RT go.
#[derive(Debug)]
pub struct Output {
    /// None during warm replay (no side effects).
    pub writer: Option<LiveWriter>,
    /// Where `<feed>.pb` is written on each emitting tick; None: no files.
    pub gtfs_rt_dir: Option<PathBuf>,
    pub lifecycle: LifecyclePolicy,
    /// One emitter per feed, kept across release switches (its entity cache: the journeys
    /// published by its last message).
    pub emitters: BTreeMap<Feed, FeedEmitter>,
    /// The alerts republished in each feed; None: no alerts.
    pub alerts: Option<AlertEntities>,
}

impl Output {
    /// One batch's effects to the writer (nothing for none, as Python's runner writes).
    pub fn write(&mut self, phases: &mut Phases, effects: &[Effect]) -> Result<(), WorkerError> {
        match &mut self.writer {
            Some(writer) if !effects.is_empty() => {
                phases.time("write", || writer.write(effects))?;
                Ok(())
            }
            _ => Ok(()),
        }
    }

    /// A tick's effects written, then the state tables and GTFS-RT files at `at`.
    pub fn ticked<L: Loads>(
        &mut self,
        runner: &mut Runner<L, Phases>,
        at: Instant,
        effects: &[Effect],
    ) -> Result<(), WorkerError> {
        self.write(&mut runner.phases, effects)?;
        self.emit(runner, at)
    }

    /// The state tables, then each feed's GTFS-RT file: the state write settles who each report
    /// is (the vehicle book), and the feed names vehicles as the state tables do.
    pub fn emit<L: Loads>(
        &mut self,
        runner: &mut Runner<L, Phases>,
        now: Instant,
    ) -> Result<(), WorkerError> {
        let mut phases = std::mem::take(&mut runner.phases);
        let result = self.emit_timed(runner, now, &mut phases);
        runner.phases = phases;
        result
    }

    fn emit_timed<L: Loads>(
        &mut self,
        runner: &Runner<L, Phases>,
        now: Instant,
        phases: &mut Phases,
    ) -> Result<(), WorkerError> {
        if let Some(writer) = &mut self.writer {
            let states: Vec<(Feed, &FeedState)> = runner
                .runtimes
                .iter()
                .map(|(feed, runtime)| (*feed, &runtime.state))
                .collect();
            phases.time("state", || writer.write_state(&states))?;
        }
        let Some(dir) = &self.gtfs_rt_dir else {
            return Ok(());
        };
        if let (Some(alerts), Some(writer)) = (&mut self.alerts, &self.writer)
            && alerts.dirty
        {
            let rows = writer
                .store
                .block_on(alert_feed_rows(writer.store.client()))?;
            alerts.load(alert_rows(rows)?);
        }
        for feed in runner.sorted_feeds() {
            let state = &runner.runtimes[&feed].state;
            let emitter = self
                .emitters
                .entry(feed)
                .or_insert_with(|| FeedEmitter::new(self.lifecycle.clone()));
            phases.time("gtfs_rt", || -> Result<(), WorkerError> {
                let alerts = match (&mut self.alerts, &self.writer) {
                    (Some(alerts), Some(_)) => alerts
                        .entities(feed, now)
                        .map_err(|e| WorkerError::Other(e.to_string()))?,
                    _ => Vec::new(),
                };
                let bytes = match &self.writer {
                    Some(writer) => {
                        let book = &writer.vehicles;
                        emitter.serialize(state, now, &alerts, &|v| book.vehicle_id(v))
                    }
                    None => emitter.serialize(state, now, &alerts, &source_form),
                };
                write_atomic(&dir.join(format!("{}.pb", feed.as_str())), &bytes)?;
                Ok(())
            })?;
        }
        Ok(())
    }
}

/// The live worker's runner, output and clock.
pub struct Pipeline<L: Loads> {
    pub runner: Runner<L, Phases>,
    pub output: Output,
    /// Housekeeping ticks on round multiples of `emit.tick_s`, each before the first
    /// observation at or after it (as replay and warm start run them).
    pub ticks: Ticks,
}

impl<L: Loads> std::fmt::Debug for Pipeline<L> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Pipeline")
            .field("runner", &self.runner)
            .field("ticks", &self.ticks)
            .finish_non_exhaustive()
    }
}

impl<L: Loads> Pipeline<L> {
    /// Every tick due up to and including `at`, each written and emitted.
    pub fn advance(&mut self, at: Instant) -> Result<(), WorkerError> {
        let Pipeline {
            runner,
            output,
            ticks,
        } = self;
        ticks.advance(runner, at, |runner, tick, effects| {
            output.ticked(runner, tick, &effects)
        })
    }

    /// One poll of a facts channel received at `at`: the ticks due before it, then its
    /// observations (none for a failed or undecodable poll) through the core and the writer.
    pub fn poll(
        &mut self,
        at: Instant,
        observations: &[Arc<Observation>],
    ) -> Result<(), WorkerError> {
        self.advance(at)?;
        if observations.is_empty() {
            return Ok(());
        }
        let effects = self.runner.process(observations)?;
        self.output.write(&mut self.runner.phases, &effects)
    }

    /// A last tick at `now`, written and emitted (on stop, as Python's `close`).
    pub fn close(&mut self, now: Instant) -> Result<(), WorkerError> {
        let effects = self.runner.tick(now)?;
        self.output.ticked(&mut self.runner, now, &effects)
    }

    /// Rebase live journeys onto a newly activated release, under its writer; alerts are
    /// resolved again (`Runner.switch_release`).
    pub fn switch_release(&mut self, loads: L, writer: LiveWriter) -> Result<(), WorkerError> {
        self.output.writer = Some(writer);
        if let Some(alerts) = &mut self.output.alerts {
            alerts.dirty = true;
        }
        let effects = self.runner.switch_release(loads)?;
        self.output.write(&mut self.runner.phases, &effects)
    }
}

/// Replay through the worker's output: each step's effects written, each tick written and
/// emitted, as the live [`Pipeline`] does (`replay::drive` runs the ticks before the polls).
#[derive(Debug)]
pub struct ReplaySink<'a> {
    pub output: &'a mut Output,
}

impl<L: Loads> Sink<L, Phases> for ReplaySink<'_> {
    fn stepped(
        &mut self,
        runner: &mut Runner<L, Phases>,
        _poll: &DecodedPoll,
        effects: Option<Vec<Effect>>,
    ) -> Result<(), ReplayError> {
        if let Some(effects) = effects {
            self.output
                .write(&mut runner.phases, &effects)
                .map_err(replay_error)?;
        }
        Ok(())
    }

    fn ticked(
        &mut self,
        runner: &mut Runner<L, Phases>,
        at: Instant,
        effects: Vec<Effect>,
    ) -> Result<(), ReplayError> {
        self.output
            .ticked(runner, at, &effects)
            .map_err(replay_error)
    }

    fn snapshot(
        &mut self,
        _runner: &mut Runner<L, Phases>,
        _at: Instant,
    ) -> Result<(), ReplayError> {
        Ok(())
    }
}

fn replay_error(error: WorkerError) -> ReplayError {
    match error {
        WorkerError::Run(e) => ReplayError::Run(e),
        WorkerError::Io(e) => ReplayError::Io(e),
        other => ReplayError::Other(other.to_string()),
    }
}

// --- alerts ----------------------------------------------------------------------------------

fn text_map(value: &Value) -> BTreeMap<String, String> {
    value
        .as_object()
        .map(|o| {
            o.iter()
                .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_owned())))
                .collect()
        })
        .unwrap_or_default()
}

fn opt_text(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}

/// `alert_feed.sql`'s rows by feed, in its order, as the alert entities read them.
pub fn alert_rows(rows: Vec<AlertFeedRow>) -> Result<BTreeMap<Feed, Vec<AlertRow>>, WorkerError> {
    let mut by_feed: BTreeMap<Feed, Vec<AlertRow>> = BTreeMap::new();
    for row in rows {
        let feed: Feed = row
            .feed
            .parse()
            .map_err(|_| WorkerError::Other(format!("alert feed {:?}", row.feed)))?;
        let list = |v: &Value| v.as_array().cloned().unwrap_or_default();
        by_feed.entry(feed).or_default().push(AlertRow {
            periods: list(&row.periods)
                .iter()
                .map(|p| AlertPeriod {
                    start: opt_text(&p["start"]),
                    end: opt_text(&p["end"]),
                })
                .collect(),
            header: text_map(&row.header),
            description: text_map(&row.description),
            url: text_map(&row.url),
            cause_detail: text_map(&row.cause_detail),
            effect_detail: text_map(&row.effect_detail),
            targets: list(&row.targets)
                .iter()
                .map(|t| AlertTarget {
                    kind: t["kind"].as_str().unwrap_or_default().to_owned(),
                    id: t["id"].as_str().unwrap_or_default().to_owned(),
                    service_date: opt_text(&t["service_date"]),
                })
                .collect(),
            source: row.source,
            alert_id: row.alert_id,
            cause: row.cause,
            effect: row.effect,
            severity: row.severity,
            entities: row.entities,
            details: row.details,
        });
    }
    Ok(by_feed)
}
