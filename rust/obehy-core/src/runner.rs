//! The one pipeline the worker and replay share: decoded observations → core → effects, without
//! I/O: the index's rows come from a [`Loads`] (Parquet or PostgreSQL, in obehy-rt), and the
//! effects go back to the caller, which writes them (writing never feeds back into the core). The GTFS-RT files and state tables of an emitting tick are the shell's too.
//!
//! Per batch it loads the static data of the batch's keys (lazy index), steps every observation
//! in `(received_at, source, sha256, item)` order and checks the runner's invariants
//! ([`RunError::Invariant`] is fatal). [`Runner::tick`] is the housekeeping step: compaction and
//! sweeps when due, then per feed (sorted) `refresh` and `estimate_all`. [`Ticks`] steps a
//! simulated clock through ticks on round multiples of `emit.tick_s` (`advance`, `split`).

use std::collections::{BTreeSet, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;

use crate::config::Policy;
use crate::core::{
    Context, CoreError, Semantics, estimate_all_with, rebase, refresh, resolve, step, vehicle_of,
};
use crate::index::{Index, IndexLoader, IndexMiss, KeyRef, LoadError, Rows, RowsError};
use crate::infer::keyed::admissible;
use crate::model::{
    Effect, Feed, FeedState, JourneyKey, LineRef, Observation, PlatformAssignment, ServiceDay,
    Track, TripKey,
};
use crate::time::{
    Date, Instant, ServiceTime, TimeDelta, candidate_service_dates, local_date, next_multiple,
};
use crate::timeline::SourceProgress;
use crate::timeline::plan::PlanCache;

/// Where a release's static rows come from, per feed.
pub trait Loads {
    type Rows: Rows;
    fn release_id(&self) -> &str;
    fn rows(&self, feed: Feed) -> Result<Self::Rows, RowsError>;
}

/// Times the runner's phases (`phases.Phases`); the shell's implementation measures CPU time.
pub trait Phases {
    fn time<T>(&mut self, name: &str, f: impl FnOnce() -> T) -> T;
    fn count(&mut self, name: &str, n: u64);
}

/// No timing.
#[derive(Debug, Default, Clone, Copy)]
pub struct Untimed;

impl Phases for Untimed {
    fn time<T>(&mut self, _name: &str, f: impl FnOnce() -> T) -> T {
        f()
    }

    fn count(&mut self, _name: &str, _n: u64) {}
}

/// The runner cannot go on: static rows could not be read, the core read static data that was
/// not loaded, or the core broke one of its own guarantees.
#[derive(Debug, thiserror::Error)]
pub enum RunError {
    #[error(transparent)]
    Load(#[from] LoadError),
    #[error(transparent)]
    Rows(#[from] RowsError),
    #[error(transparent)]
    Core(#[from] CoreError),
    #[error("invariant: {0}")]
    Invariant(String),
}

impl From<IndexMiss> for RunError {
    fn from(miss: IndexMiss) -> RunError {
        RunError::Core(CoreError::IndexMiss(miss))
    }
}

/// One feed's observations of a `process` call, in processing order, and the keys, lines and
/// service dates whose static data they need.
#[derive(Debug, Clone)]
pub struct Batch {
    pub feed: Feed,
    pub observations: Vec<Arc<Observation>>,
    pub refs: BTreeSet<KeyRef>,
    pub lines: BTreeSet<KeyRef>,
    pub days: BTreeSet<Date>,
}

/// One feed's index, state and caches.
#[derive(Debug)]
pub struct FeedRuntime<R> {
    pub loader: IndexLoader<R>,
    pub state: FeedState,
    pub plans: PlanCache,
    pub progress: SourceProgress,
}

impl<R> FeedRuntime<R> {
    /// The state and the core's context over this feed's index.
    pub fn split<'a>(
        &'a mut self,
        policy: &'a Policy,
        semantics: &'a Semantics,
    ) -> (&'a mut FeedState, Context<'a>) {
        (
            &mut self.state,
            Context {
                index: &mut self.loader.index,
                plans: &mut self.plans,
                progress: &mut self.progress,
                policy,
                semantics,
            },
        )
    }

    pub fn index(&self) -> &Index {
        &self.loader.index
    }
}

fn refs(observation: &Observation) -> impl Iterator<Item = KeyRef> + '_ {
    observation
        .all::<TripKey>()
        .map(|key| KeyRef::new(&key.namespace, &key.key))
}

/// Lines whose trips a keyless observation is inferred among.
fn lines(observation: &Observation) -> Vec<KeyRef> {
    if observation.first::<TripKey>().is_some() {
        return Vec::new();
    }
    observation
        .all::<LineRef>()
        .map(|line| KeyRef::new(&line.namespace, &line.value))
        .collect()
}

/// The service dates binding the observation may look at: around its time, its source's
/// service day, and around a board row's scheduled time.
pub fn observation_days(observation: &Observation) -> BTreeSet<Date> {
    let mut days: BTreeSet<Date> = candidate_service_dates(observation.at())
        .into_iter()
        .collect();
    if let Some(platform) = observation.first::<PlatformAssignment>() {
        days.extend(candidate_service_dates(platform.scheduled));
    }
    if let Some(day) = observation.first::<ServiceDay>() {
        days.insert(day.day);
    }
    days
}

pub struct Runner<L: Loads, P: Phases = Untimed> {
    pub loads: L,
    pub policy: Arc<Policy>,
    pub semantics: Semantics,
    /// In the order of the feeds given.
    pub runtimes: IndexMap<Feed, FeedRuntime<L::Rows>>,
    pub skipped: u64,
    pub phases: P,
    pub next_compaction: Option<Instant>,
    pub next_sweep: Option<Instant>,
    /// Threads estimating a feed's journeys (`estimate_all_with`; the output does not depend
    /// on it). 1 by default.
    pub estimate_threads: usize,
}

impl<L: Loads, P: Phases> std::fmt::Debug for Runner<L, P> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Runner")
            .field("release_id", &self.loads.release_id())
            .field("feeds", &self.runtimes.keys().collect::<Vec<_>>())
            .field("skipped", &self.skipped)
            .field("next_compaction", &self.next_compaction)
            .field("next_sweep", &self.next_sweep)
            .finish_non_exhaustive()
    }
}

impl<L: Loads, P: Phases> Runner<L, P> {
    pub fn new(
        loads: L,
        policy: Arc<Policy>,
        feeds: &[Feed],
        semantics: Semantics,
        phases: P,
    ) -> Result<Self, RunError> {
        let mut runtimes = IndexMap::new();
        for &feed in feeds {
            let runtime = runtime(&loads, feed, FeedState::new(feed), &[])?;
            runtimes.insert(feed, runtime);
        }
        Ok(Runner {
            loads,
            policy,
            semantics,
            runtimes,
            skipped: 0,
            phases,
            next_compaction: None,
            next_sweep: None,
            estimate_threads: 1,
        })
    }

    /// The feeds, sorted.
    pub fn sorted_feeds(&self) -> Vec<Feed> {
        let mut feeds: Vec<Feed> = self.runtimes.keys().copied().collect();
        feeds.sort();
        feeds
    }

    /// Load the static data of the observations' keys ahead of processing them.
    pub fn prefetch(&mut self, observations: &[Arc<Observation>]) -> Result<(), RunError> {
        let mut wanted: std::collections::BTreeMap<Feed, Batch> = std::collections::BTreeMap::new();
        for observation in observations {
            if !self.runtimes.contains_key(&observation.feed) {
                continue;
            }
            let batch = wanted
                .entry(observation.feed)
                .or_insert_with(|| empty(observation.feed));
            batch.refs.extend(refs(observation));
            batch.lines.extend(lines(observation));
            batch.days.extend(observation_days(observation));
        }
        for (feed, batch) in wanted {
            let runtime = &mut self.runtimes[&feed];
            self.phases.time("index", || {
                runtime
                    .loader
                    .ensure(&batch.refs, batch.days.iter().copied(), &batch.lines)
                    .map(|_| ())
            })?;
        }
        Ok(())
    }

    pub fn process(&mut self, observations: &[Arc<Observation>]) -> Result<Vec<Effect>, RunError> {
        let batches = self.batches(observations);
        self.load(&batches)?;
        self.run(&batches)
    }

    /// The observations per feed, in processing order, with the static data they name.
    pub fn batches(&mut self, observations: &[Arc<Observation>]) -> Vec<Batch> {
        let mut by_feed: std::collections::BTreeMap<Feed, Batch> =
            std::collections::BTreeMap::new();
        for observation in observations {
            if self.runtimes.contains_key(&observation.feed) {
                by_feed
                    .entry(observation.feed)
                    .or_insert_with(|| empty(observation.feed))
                    .observations
                    .push(observation.clone());
            } else {
                self.skipped += 1;
            }
        }
        let mut batches: Vec<Batch> = by_feed.into_values().collect();
        for batch in &mut batches {
            // Stable: full ties keep their input order.
            batch.observations.sort_by(|a, b| {
                (a.received_at, &a.source, &a.raw.sha256, a.raw.item).cmp(&(
                    b.received_at,
                    &b.source,
                    &b.raw.sha256,
                    b.raw.item,
                ))
            });
            for observation in &batch.observations {
                batch.refs.extend(refs(observation));
                batch.lines.extend(lines(observation));
                batch.days.extend(observation_days(observation));
            }
        }
        batches
    }

    /// Whether `load` would read rows (the worker then runs it off its loop).
    pub fn needs_index(&self, batches: &[Batch]) -> bool {
        batches.iter().any(|b| {
            self.runtimes[&b.feed]
                .loader
                .needs(&b.refs, b.days.iter().copied(), &b.lines)
        })
    }

    /// Load the static data the batches name that is not in the index yet.
    pub fn load(&mut self, batches: &[Batch]) -> Result<(), RunError> {
        for batch in batches {
            let runtime = &mut self.runtimes[&batch.feed];
            self.phases.time("index", || {
                runtime
                    .loader
                    .ensure(&batch.refs, batch.days.iter().copied(), &batch.lines)
                    .map(|_| ())
            })?;
        }
        Ok(())
    }

    /// Step the loaded batches through the core; their effects, for the caller to write.
    pub fn run(&mut self, batches: &[Batch]) -> Result<Vec<Effect>, RunError> {
        let mut effects: Vec<Effect> = Vec::new();
        let Runner {
            policy,
            semantics,
            runtimes,
            phases,
            ..
        } = self;
        for batch in batches {
            let runtime = &mut runtimes[&batch.feed];
            let last = batch.observations.last().expect("a batch has observations");
            runtime
                .loader
                .touch(&batch.refs, &batch.lines, last.received_at);
            phases.time("core", || -> Result<(), RunError> {
                for observation in &batch.observations {
                    effects.extend(step_checked(runtime, observation, policy, semantics)?);
                }
                Ok(())
            })?;
            phases.count("observations", batch.observations.len() as u64);
        }
        Ok(effects)
    }

    /// Housekeeping and estimates at `now`: compaction and sweeps when due, then per feed
    /// (sorted) `refresh` and `estimate_all`. Housekeeping's history effects (a terminus
    /// arrival inferred) are returned for the caller to write.
    pub fn tick(&mut self, now: Instant) -> Result<Vec<Effect>, RunError> {
        match self.next_compaction {
            None => self.next_compaction = Some(self.compaction_after(now)),
            Some(at) if now >= at => {
                self.compact()?;
                self.next_compaction = Some(self.compaction_after(now));
            }
            Some(_) => {}
        }
        let every = TimeDelta::from_seconds(self.policy.emit.sweep_index_every_s);
        match self.next_sweep {
            None => self.next_sweep = Some(now + every),
            Some(at) if now >= at => {
                self.sweep(now);
                self.next_sweep = Some(now + every);
            }
            Some(_) => {}
        }
        let mut effects: Vec<Effect> = Vec::new();
        for feed in self.sorted_feeds() {
            let Runner {
                policy,
                semantics,
                runtimes,
                phases,
                estimate_threads,
                ..
            } = self;
            let runtime = &mut runtimes[&feed];
            let (state, mut ctx) = runtime.split(policy, semantics);
            effects.extend(phases.time("refresh", || refresh(state, now, &mut ctx))?);
            let threads = *estimate_threads;
            phases.time("estimate", || {
                estimate_all_with(state, &mut ctx, now, threads)
            })?;
        }
        Ok(effects)
    }

    /// `estimate_all` of every feed (sorted) at `now`: before a snapshot.
    pub fn estimate(&mut self, now: Instant) -> Result<(), RunError> {
        for feed in self.sorted_feeds() {
            let Runner {
                policy,
                semantics,
                runtimes,
                estimate_threads,
                ..
            } = self;
            let (state, mut ctx) = runtimes[&feed].split(policy, semantics);
            estimate_all_with(state, &mut ctx, now, *estimate_threads)?;
        }
        Ok(())
    }

    fn compaction_after(&self, now: Instant) -> Instant {
        let mut day = local_date(now);
        let at_s =
            i32::try_from(self.policy.emit.compact_index_at_s).expect("compact_index_at_s fits");
        loop {
            let at = ServiceTime::new(day, at_s).instant();
            if at > now {
                return at;
            }
            day = day.add_days(1);
        }
    }

    /// Drop the static data and plans of keys not named for `keep_index_s` that no live journey
    /// needs, in place (BASE_PLAN.md section 18.3); they reload when named again. Loading is
    /// deterministic, so this never changes output.
    pub fn sweep(&mut self, now: Instant) {
        let since = now - TimeDelta::from_seconds(self.policy.emit.keep_index_s);
        let yesterday = local_date(now).add_days(-1);
        for runtime in self.runtimes.values_mut() {
            let state = &runtime.state;
            let bindings: Vec<_> = state
                .vehicles
                .values()
                .filter_map(|v| v.binding.as_ref())
                .collect();
            let mut journeys: HashSet<&JourneyKey> = state.instances.keys().collect();
            journeys.extend(bindings.iter().map(|b| &b.journey));
            let mut trip_ids: HashSet<&str> = state
                .instances
                .values()
                .map(|i| i.trip_id.as_str())
                .collect();
            trip_ids.extend(bindings.iter().map(|b| b.trip_id.as_str()));
            let day = journeys
                .iter()
                .map(|j| j.service_date)
                .fold(yesterday, |a, b| if b < a { b } else { a });
            let keys: Vec<KeyRef> = journeys
                .iter()
                .map(|j| KeyRef::new(&j.namespace, &j.key))
                .collect();
            runtime
                .loader
                .sweep(since, trip_ids.iter().copied(), keys.iter(), day);
            let trips = &runtime.loader.index.trips;
            runtime.plans.sweep(|t| trips.contains_key(t), day);
        }
    }

    /// Drop the static data and plans no live journey needs (BASE_PLAN.md section 18.3): a
    /// fresh index with just the live journeys' data replaces each feed's. Loading is
    /// deterministic, so this never changes output.
    pub fn compact(&mut self) -> Result<(), RunError> {
        for feed in self.sorted_feeds() {
            let old = self.runtimes.get_mut(&feed).expect("a feed");
            let state = std::mem::replace(&mut old.state, FeedState::new(feed));
            let mut journeys: BTreeSet<JourneyKey> = state.instances.keys().cloned().collect();
            journeys.extend(
                state
                    .vehicles
                    .values()
                    .filter_map(|v| v.binding.as_ref().map(|b| b.journey.clone())),
            );
            let journeys: Vec<JourneyKey> = journeys.into_iter().collect();
            let mut fresh = runtime(&self.loads, feed, state, &journeys)?;
            for journey in &journeys {
                resolve(journey, &mut fresh.loader.index)?; // rebuilds the run trips of rail journeys
            }
            self.runtimes[&feed] = fresh;
        }
        Ok(())
    }

    /// Rebase live journeys onto a newly activated release (mid-day activation).
    pub fn switch_release(&mut self, loads: L) -> Result<Vec<Effect>, RunError> {
        self.loads = loads;
        let mut effects = Vec::new();
        for feed in self.sorted_feeds() {
            let old = self.runtimes.get_mut(&feed).expect("a feed");
            let mut state = std::mem::replace(&mut old.state, FeedState::new(feed));
            let mut journeys: Vec<JourneyKey> = state.instances.keys().cloned().collect();
            journeys.sort();
            let mut fresh = runtime(&self.loads, feed, FeedState::new(feed), &journeys)?;
            effects.extend(rebase(&mut state, &mut fresh.loader.index)?);
            fresh.state = state;
            self.runtimes[&feed] = fresh;
        }
        Ok(effects)
    }
}

fn empty(feed: Feed) -> Batch {
    Batch {
        feed,
        observations: Vec::new(),
        refs: BTreeSet::new(),
        lines: BTreeSet::new(),
        days: BTreeSet::new(),
    }
}

/// A feed's runtime over `loads`: a fresh index, loaded for `journeys` first.
fn runtime<L: Loads>(
    loads: &L,
    feed: Feed,
    state: FeedState,
    journeys: &[JourneyKey],
) -> Result<FeedRuntime<L::Rows>, RunError> {
    let mut loader = IndexLoader::new(loads.rows(feed)?, loads.release_id(), feed);
    if !journeys.is_empty() {
        let refs: BTreeSet<KeyRef> = journeys
            .iter()
            .map(|j| KeyRef::new(&j.namespace, &j.key))
            .collect();
        let days: BTreeSet<Date> = journeys.iter().map(|j| j.service_date).collect();
        loader.ensure(&refs, days, std::iter::empty())?;
    }
    Ok(FeedRuntime {
        loader,
        state,
        plans: PlanCache::new(),
        progress: SourceProgress::new(),
    })
}

/// `core.step` plus the runner's invariants: committed progress never goes back, and a binding
/// is never re-dated while still admissible.
fn step_checked<R>(
    runtime: &mut FeedRuntime<R>,
    observation: &Arc<Observation>,
    policy: &Policy,
    semantics: &Semantics,
) -> Result<Vec<Effect>, RunError> {
    let vehicle = vehicle_of(observation);
    let before = vehicle
        .as_ref()
        .and_then(|v| runtime.state.vehicles.get(v))
        .and_then(|v| v.binding.clone());
    let old_committed = before
        .as_ref()
        .and_then(|b| runtime.state.instances.get(&b.journey))
        .and_then(|i| i.track.as_ref())
        .and_then(Track::committed);
    let (state, mut ctx) = runtime.split(policy, semantics);
    let effects = step(state, observation, &mut ctx)?;
    let after = vehicle
        .as_ref()
        .and_then(|v| state.vehicles.get(v))
        .and_then(|v| v.binding.as_ref());
    let (Some(old), Some(new)) = (before, after) else {
        return Ok(effects);
    };
    if old.journey == new.journey {
        // The live position may be revised between hypotheses; committed progress never. A
        // track that had not started had committed nothing: starting, it may commit behind 0
        // (a doubled origin's departure lies before the path's start).
        let track = state.instances[&new.journey].track.as_ref();
        if let (Some(old_committed), Some(track)) = (old_committed, track)
            && track.committed_m < old_committed
        {
            return Err(RunError::Invariant(format!(
                "committed progress of {:?} went back",
                new.journey
            )));
        }
    } else if old.journey.namespace == new.journey.namespace
        && old.journey.key == new.journey.key
        && admissible(&old, observation.at(), ctx.index, policy)?
    {
        return Err(RunError::Invariant(format!(
            "{:?} was re-dated while still admissible",
            old.journey
        )));
    }
    Ok(effects)
}

/// A simulated clock's housekeeping ticks, as the worker's wall clock runs them: every
/// `emit.tick_s` on round multiples, each before the first observation after it. Replay, warm
/// start and bench all step through time with it.
#[derive(Debug, Clone)]
pub struct Ticks {
    pub every_s: i64,
    pub next: Option<Instant>,
}

impl Ticks {
    pub fn new(policy: &Policy) -> Ticks {
        Ticks {
            every_s: policy.emit.tick_s,
            next: None,
        }
    }

    pub fn due(&self, at: Instant) -> bool {
        self.next.is_some_and(|next| next <= at)
    }

    /// Run every tick up to and including `at`; `ticked` sees each tick's time and effects
    /// after it ran.
    pub fn advance<L: Loads, P: Phases, E: From<RunError>>(
        &mut self,
        runner: &mut Runner<L, P>,
        at: Instant,
        mut ticked: impl FnMut(&mut Runner<L, P>, Instant, Vec<Effect>) -> Result<(), E>,
    ) -> Result<(), E> {
        let mut next = *self
            .next
            .get_or_insert_with(|| next_multiple(at, self.every_s));
        while next <= at {
            let effects = runner.tick(next)?;
            ticked(runner, next, effects)?;
            next = next_multiple(next, self.every_s);
            self.next = Some(next);
        }
        Ok(())
    }

    /// The chunk (in `received_at` order) in runs between ticks, each handed to `run`; the
    /// ticks between them run on the way, `ticked` seeing each as in [`Ticks::advance`]
    ///. The first observation starts the clock.
    pub fn split<L: Loads, P: Phases, E: From<RunError>>(
        &mut self,
        runner: &mut Runner<L, P>,
        chunk: &[Arc<Observation>],
        mut ticked: impl FnMut(&mut Runner<L, P>, Instant, Vec<Effect>) -> Result<(), E>,
        mut run: impl FnMut(&mut Runner<L, P>, &[Arc<Observation>]) -> Result<(), E>,
    ) -> Result<(), E> {
        let mut start = 0;
        for (i, observation) in chunk.iter().enumerate() {
            if self.next.is_none() || self.due(observation.received_at) {
                if i > start {
                    run(runner, &chunk[start..i])?;
                }
                start = i;
                self.advance(runner, observation.received_at, &mut ticked)?;
            }
        }
        if start < chunk.len() {
            run(runner, &chunk[start..])?;
        }
        Ok(())
    }
}
