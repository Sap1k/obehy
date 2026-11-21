//! `obehy-rt compare --level output`: everything the worker would write from a window (the
//! record kinds, their order and the `at` of each are in [`crate::compare`]'s module doc). Every
//! effect of every step and tick (`phase` `step`,
//! `tick`, `finish`) in the order produced, each followed by the rows the writer made of it;
//! every `gtfs_rt_every_s` (on round multiples) estimates for every feed, the state tables and
//! a `gtfs_rt` record per feed; after the last poll the final tick's effects and rows.

use std::collections::BTreeMap;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;

use obehy_core::config::policy::LifecyclePolicy;
use obehy_core::config::{Policy, Source};
use obehy_core::core::{CORE_VERSION, Semantics};
use obehy_core::model::{Derivation, Effect, Feed, FeedState, VehicleId};
use obehy_core::publish::gtfs_rt::{FeedEmitter, source_form};
use obehy_core::runner::{Loads, Runner, Ticks};
use obehy_core::time::Instant;
use obehy_core::vehicles::vocabulary::VOCABULARY_FILE;
use obehy_core::vehicles::{VehicleBook, Vocabulary};
use obehy_sources::Decoders;

use crate::canonical::{obj, text, time};
use crate::compare::{Window, header};
use crate::gtfs_json::message_dict;
use crate::phases::Phases;
use crate::plain::effect_plain;
use crate::replay::{
    Clock, DecodeTimes, DecodedPoll, FileLoads, ReplayError, Sink, decoded_polls, drive, finish,
};
use crate::samples::{JourneySampler, PREDICTOR_SAMPLE_EVERY_S, predictor_sample};
use crate::settings::Paths;
use crate::store::vehicles::{Book, PlainBook, StoredVehicles, book, physical_rows};
use crate::store::{RecordingStore, Writer};
use serde_json::Value;

#[derive(Debug, Clone)]
pub struct Options {
    pub release: PathBuf,
    pub feeds: Vec<Feed>,
    pub gtfs_rt_every_s: Option<i64>,
    /// Threads decoding ahead of the core (1: inline).
    pub threads: usize,
    /// `poll` and `predictor_sample` records (what the replay report reads).
    pub diagnostics: bool,
    /// `journey_sample` records too (implies `diagnostics`).
    pub quality: bool,
}

pub type Emit<'a> = &'a mut dyn FnMut(&Value) -> io::Result<()>;

/// The channels' core semantics, by (source, channel).
pub fn semantics_of(window: &Window) -> Semantics {
    window
        .channels
        .iter()
        .map(|c| ((c.source.clone(), c.channel.clone()), c.core_semantics))
        .collect()
}

pub fn io_error(error: impl std::fmt::Display) -> io::Error {
    io::Error::other(error.to_string())
}

/// How the vehicle book names a report in GTFS-RT and the state tables
/// (`VehicleBook.vehicle_id`).
pub trait Named {
    fn vehicle_id(&self, vehicle: &VehicleId) -> String;
}

impl Named for PlainBook {
    fn vehicle_id(&self, vehicle: &VehicleId) -> String {
        source_form(vehicle)
    }
}

impl Named for VehicleBook {
    fn vehicle_id(&self, vehicle: &VehicleId) -> String {
        VehicleBook::vehicle_id(self, vehicle)
    }
}

/// The book the harness writes with: the worker's vehicle book over an empty database.
pub type HarnessBook = VehicleBook;

/// The vehicle book as a recording store opens it, with the shipped vocabulary.
pub fn harness_book() -> io::Result<impl FnOnce(StoredVehicles) -> HarnessBook> {
    let paths = Paths::discover().map_err(io_error)?;
    let vocabulary =
        Vocabulary::load(&paths.data().join("..").join(VOCABULARY_FILE)).map_err(io_error)?;
    Ok(move |stored| book(stored, vocabulary))
}

/// `record | {"at": at}`.
pub fn with_at(record: Value, at: Option<Instant>) -> Value {
    match record {
        Value::Object(mut fields) => {
            fields.shift_remove("at");
            fields.insert("at".to_owned(), time(at));
            Value::Object(fields)
        }
        other => other,
    }
}

/// A writer into a recording store: the rows the worker would send, as records.
#[derive(Debug)]
pub struct Recording<B: Book> {
    pub writer: Writer<RecordingStore, B>,
}

impl<B: Book + Named> Recording<B> {
    pub fn open(
        sources: &[Source],
        derivation: Derivation,
        book: impl FnOnce(StoredVehicles) -> B,
    ) -> io::Result<Recording<B>> {
        let store = RecordingStore::new(physical_rows(sources));
        let writer = Writer::open(store, derivation, book).map_err(io_error)?;
        Ok(Recording { writer })
    }

    /// The rows made since the last flush, each with `at`.
    pub fn flush(&mut self, at: Option<Instant>, emit: Emit) -> io::Result<()> {
        for record in self.writer.store.take() {
            emit(&with_at(record, at))?;
        }
        Ok(())
    }

    /// One batch's effects (nothing for none, as the runner writes).
    pub fn write(&mut self, effects: &[Effect]) -> io::Result<()> {
        if effects.is_empty() {
            return Ok(());
        }
        self.writer.write(effects).map_err(io_error)
    }

    pub fn write_state(&mut self, states: &[(Feed, &FeedState)]) -> io::Result<()> {
        self.writer.write_state(states).map_err(io_error)
    }

    /// The `gtfs_rt` record of a feed at `at` (a fresh emitter), vehicles named as the
    /// writer's state rows.
    pub fn gtfs_rt(
        &self,
        state: &FeedState,
        at: Instant,
        policy: &LifecyclePolicy,
    ) -> io::Result<Value> {
        let book = &self.writer.vehicles;
        let identity = |v: &VehicleId| book.vehicle_id(v);
        let payload =
            FeedEmitter::new(policy.clone()).serialize::<&[u8]>(state, at, &[], &identity);
        Ok(obj([
            ("kind", text("gtfs_rt")),
            ("at", time(Some(at))),
            ("feed", text(state.feed.as_str())),
            ("message", message_dict(&payload).map_err(io_error)?),
        ]))
    }
}

/// A step's or tick's effects as `effect` records with their `phase`.
pub fn effect_records(emit: Emit, at: Instant, phase: &str, effects: &[Effect]) -> io::Result<()> {
    for effect in effects {
        emit(&obj([
            ("kind", text("effect")),
            ("at", time(Some(at))),
            ("phase", text(phase)),
            ("effect", effect_plain(effect)),
        ]))?;
    }
    Ok(())
}

fn states<L: Loads, P: obehy_core::runner::Phases>(
    runner: &Runner<L, P>,
) -> Vec<(Feed, &FeedState)> {
    runner
        .runtimes
        .iter()
        .map(|(feed, runtime)| (*feed, &runtime.state))
        .collect()
}

struct Recorder<'a, B: Book> {
    emit: Emit<'a>,
    recording: Recording<B>,
    policy: Arc<Policy>,
    diagnostics: bool,
    sampler: Option<JourneySampler>,
}

/// A poll's `poll` record at the output level, with its observations per feed
/// (`replay.poll_record(output=True)`).
pub fn poll_record(poll: &DecodedPoll) -> Value {
    let field = |k: &str| poll.entry.get(k).map_or(Value::Null, Value::clone);
    let request = match poll.entry.get("request") {
        Some(r @ serde_json::Value::Object(_)) => r.clone(),
        _ => Value::Null,
    };
    let mut record = obj([
        ("kind", text("poll")),
        ("at", time(Some(poll.at))),
        ("source", text(&poll.source)),
        ("channel", text(&poll.channel)),
        ("sha256", field("sha256")),
        ("status", field("status")),
        ("error", field("error")),
        ("request", request),
        ("decoder_version", Value::from(poll.decoder_version)),
    ]);
    match &poll.observations {
        None => {
            let outcome = if poll.error.is_none() {
                "failed"
            } else {
                "undecodable"
            };
            record["outcome"] = text(outcome);
        }
        Some(observations) => {
            let mut feeds: BTreeMap<&str, usize> = BTreeMap::new();
            for o in observations {
                *feeds.entry(o.feed.as_str()).or_default() += 1;
            }
            record["outcome"] = text("decoded");
            record["observations"] = Value::from(observations.len());
            record["feeds"] = serde_json::json!(feeds);
        }
    }
    record
}

type Timed = Runner<FileLoads, Phases>;

impl<B: Book + Named> Recorder<'_, B> {
    fn timed_effects(
        &mut self,
        runner: &mut Timed,
        at: Instant,
        phase: &str,
        effects: &[Effect],
    ) -> io::Result<()> {
        runner
            .phases
            .time("write", || self.recording.write(effects))?;
        runner.phases.time("emit", || -> io::Result<()> {
            effect_records(self.emit, at, phase, effects)?;
            self.recording.flush(Some(at), self.emit)
        })
    }
}

impl<B: Book + Named> Sink<FileLoads, Phases> for Recorder<'_, B> {
    fn stepped(
        &mut self,
        runner: &mut Timed,
        poll: &DecodedPoll,
        effects: Option<Vec<Effect>>,
    ) -> Result<(), ReplayError> {
        if self.diagnostics {
            (self.emit)(&poll_record(poll))?;
        }
        if let Some(effects) = effects {
            self.timed_effects(runner, poll.at, "step", &effects)?;
        }
        Ok(())
    }

    fn ticked(
        &mut self,
        runner: &mut Timed,
        at: Instant,
        effects: Vec<Effect>,
    ) -> Result<(), ReplayError> {
        self.timed_effects(runner, at, "tick", &effects)?;
        Ok(())
    }

    fn sampled(&mut self, runner: &mut Timed, at: Instant) -> Result<(), ReplayError> {
        let Some(sampler) = &mut self.sampler else {
            return Ok(());
        };
        for feed in runner.sorted_feeds() {
            let Runner {
                policy,
                semantics,
                runtimes,
                ..
            } = runner;
            let (state, mut ctx) = runtimes[&feed].split(policy, semantics);
            let record = sampler
                .sample(state, &mut ctx, at)
                .map_err(obehy_core::runner::RunError::from)?;
            (self.emit)(&record)?;
        }
        Ok(())
    }

    fn predictors(&mut self, runner: &mut Timed, at: Instant) -> Result<(), ReplayError> {
        let rail = &runner.runtimes[&Feed::Czptt];
        let record = predictor_sample(&rail.state, rail.index(), at)
            .map_err(obehy_core::runner::RunError::from)?;
        (self.emit)(&record)?;
        Ok(())
    }

    fn snapshot(&mut self, runner: &mut Timed, at: Instant) -> Result<(), ReplayError> {
        let mut phases = std::mem::take(&mut runner.phases);
        phases.time("estimate", || runner.estimate(at))?;
        phases.time("state", || self.recording.write_state(&states(runner)))?;
        phases.time("emit", || self.recording.flush(Some(at), self.emit))?;
        for feed in runner.sorted_feeds() {
            let state = &runner.runtimes[&feed].state;
            let record = phases.time("gtfs_rt", || {
                self.recording.gtfs_rt(state, at, &self.policy.lifecycle)
            })?;
            phases.time("emit", || (self.emit)(&record))?;
        }
        runner.phases = phases;
        Ok(())
    }
}

/// The output level (`compare.output`).
pub fn output(
    window: &Window,
    options: &Options,
    decoders: &Decoders,
    sources: &[Source],
    policy: Policy,
    emit: Emit,
) -> io::Result<()> {
    let loads = FileLoads::open(&options.release).map_err(io_error)?;
    let policy = Arc::new(policy);
    let mut runner = Runner::new(
        loads,
        policy.clone(),
        &options.feeds,
        semantics_of(window),
        Phases::new(),
    )
    .map_err(io_error)?;
    runner.estimate_threads = options.threads.max(1);
    output_from(runner, window, options, decoders, sources, emit)
}

/// The output level from a runner already made (a warm-started one: `obehy-rt warm --then`).
pub fn output_from(
    mut runner: Runner<FileLoads, Phases>,
    window: &Window,
    options: &Options,
    decoders: &Decoders,
    sources: &[Source],
    emit: Emit,
) -> io::Result<()> {
    let policy = runner.policy.clone();
    let loads = runner.loads.clone();
    let mut head = header(window, "output");
    if let Value::Object(fields) = &mut head {
        fields.extend([
            (
                "feeds".to_owned(),
                Value::Array(options.feeds.iter().map(|f| text(f.as_str())).collect()),
            ),
            (
                "gtfs_rt_every_s".to_owned(),
                options.gtfs_rt_every_s.map_or(Value::Null, Value::from),
            ),
            ("release".to_owned(), text(&loads.run_id)),
            ("core_version".to_owned(), text(CORE_VERSION)),
            ("policy_version".to_owned(), text(&policy.version)),
        ]);
        if options.diagnostics || options.quality {
            fields.insert("diagnostics".to_owned(), Value::Bool(true));
        }
        if options.quality {
            fields.insert("quality".to_owned(), Value::Bool(true));
        }
    }
    emit(&head)?;
    let skew = policy.time.max_clock_skew();
    let derivation = Derivation {
        core_version: CORE_VERSION.to_owned(),
        policy_version: policy.version.clone(),
        release_id: loads.run_id.clone(),
    };
    let mut recording = Recording::open(sources, derivation, harness_book()?)?;
    let forget = &policy.emit;
    recording
        .writer
        .forget_reports(forget.forget_reports_s, forget.sweep_index_every_s);
    recording.flush(None, emit)?; // what opening the writer writes
    let mut sink = Recorder {
        emit,
        recording,
        policy: policy.clone(),
        diagnostics: options.diagnostics || options.quality,
        sampler: options.quality.then(JourneySampler::new),
    };
    let mut clock = Clock::new(Ticks::new(&policy), options.gtfs_rt_every_s);
    if sink.diagnostics && runner.runtimes.contains_key(&Feed::Czptt) {
        clock.sample_every_s = Some(PREDICTOR_SAMPLE_EVERY_S);
    }
    let decode_phases = &DecodeTimes::default();
    let mut run = || -> Result<(), ReplayError> {
        let last = drive(
            &mut runner,
            |each| decoded_polls(window, decoders, skew, options.threads, each, decode_phases),
            &mut clock,
            &mut sink,
        )?;
        if let Some(last) = last {
            let produced = finish(&mut runner, last)?;
            sink.recording.write(&produced)?;
            sink.recording.write_state(&states(&runner))?;
            effect_records(sink.emit, last, "finish", &produced)?;
            sink.recording.flush(Some(last), sink.emit)?;
        }
        Ok(())
    };
    let started = std::time::Instant::now();
    let outcome = run();
    report_phases(
        &runner.phases,
        decode_phases,
        started.elapsed().as_secs_f64(),
    );
    outcome.map_err(|e| match e {
        ReplayError::Io(e) => e,
        other => io_error(other),
    })
}

/// The run's CPU seconds per phase, on stderr.
fn report_phases(phases: &Phases, decode: &DecodeTimes, wall_s: f64) {
    let mut parts: Vec<String> = phases
        .cpu_s
        .iter()
        .map(|(name, cpu)| format!("{name} {cpu:.2}"))
        .collect();
    parts.push(format!("decode {:.2} (all threads)", decode.seconds()));
    let observations = phases.counts.get("observations").copied().unwrap_or(0);
    eprintln!(
        "{observations} observations in {wall_s:.1} s wall; cpu s: {}",
        parts.join(", ")
    );
}
