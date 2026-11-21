//! `obehy-rt corpus-stream`: the output-level stream of each corpus journey
//! (`tests/realtime/corpus/<name>.json.gz`), the same as `tests/realtime/corpus_stream.py`
//! writes into `tests/golden/corpus-stream/<name>.jsonl.gz` (read its docstring), so the core
//! is compared journey by journey.
//!
//! Each journey runs as `corpus.run` runs it: its observations in `(received_at, source,
//! item)` order, a tick every `emit.tick_s` on round multiples from the first observation until
//! 35 min after the last, each tick refreshing and then estimating, with the shipped
//! manifests' semantics and the default policy. A trace is an index slice plus the journey's
//! observations, read as `tests/realtime/traces.py`'s `from_json` reads it.

use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;

use clap::Args;
use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::config::{Policy, Source};
use obehy_core::core::{CORE_VERSION, Context, Semantics, estimate_all, refresh, step};
use obehy_core::index::{Call, DateSet, Index, KeyEntry, KeyRef, Location, Shape, Trip};
use obehy_core::model::json::facts_from_json;
use obehy_core::model::{Derivation, Effect, Feed, FeedState, Observation, RawRef};
use obehy_core::time::{Date, Instant, TimeDelta, next_multiple};
use obehy_core::timeline::SourceProgress;
use obehy_core::timeline::plan::PlanCache;
use serde_json::Value;

use crate::canonical::C;
use crate::compare::{diff, write_stream};
use crate::output::{Emit, HarnessBook, Recording, effect_records, harness_book};
use crate::settings::Paths;

/// Ticks after the last observation: stale, lost, finished.
pub const AFTER_S: i64 = 35 * 60;
/// State rows and GTFS-RT on ticks that are a multiple of this.
pub const STATE_EVERY_S: i64 = 60;

#[derive(Debug, Clone, Args)]
pub struct CorpusArgs {
    /// The corpus traces (default: tests/realtime/corpus).
    #[arg(long)]
    pub corpus: Option<PathBuf>,
    /// Where to write each journey's stream (`<name>.jsonl.gz`).
    #[arg(long)]
    pub out: PathBuf,
    /// Then diff each against the pinned stream here (default: tests/golden/corpus-stream).
    #[arg(long)]
    pub check: bool,
    #[arg(long)]
    pub reference: Option<PathBuf>,
    /// `KIND.FIELD` left out of the comparison.
    #[arg(long)]
    pub ignore: Vec<String>,
    #[arg(long, default_value_t = 5)]
    pub limit: usize,
    /// Journeys (default: all).
    pub names: Vec<String>,
}

fn s(value: &Value) -> &str {
    value.as_str().expect("a string")
}

fn date(value: &Value) -> Date {
    Date::from_isoformat(s(value)).expect("an ISO date")
}

fn int(value: &Value) -> i64 {
    value.as_i64().expect("an integer")
}

pub fn read_gz_json(path: &Path) -> io::Result<Value> {
    let file = std::fs::File::open(path)?;
    let mut text = String::new();
    flate2::read::GzDecoder::new(file).read_to_string(&mut text)?;
    serde_json::from_str(&text).map_err(io::Error::other)
}

fn observation(o: &Value) -> Observation {
    Observation {
        source: s(&o["source"]).to_owned(),
        channel: s(&o["channel"]).to_owned(),
        feed: s(&o["feed"]).parse::<Feed>().expect("a feed"),
        received_at: Instant::from_isoformat(s(&o["received_at"])).expect("an instant"),
        observed_at: o["observed_at"]
            .as_str()
            .map(|t| Instant::from_isoformat(t).expect("an instant")),
        raw: RawRef {
            sha256: s(&o["sha256"]).to_owned(),
            item: int(&o["item"]),
        },
        decoder_version: int(&o["decoder_version"]),
        facts: facts_from_json(&o["facts"]).expect("facts"),
    }
}

fn items(value: Option<&Value>) -> impl Iterator<Item = &Value> {
    value.and_then(Value::as_array).into_iter().flatten()
}

/// A trace's index and observations (`traces.from_json`).
pub fn from_json(data: &Value) -> (Index, Vec<Observation>) {
    let feed = s(&data["feed"]).parse::<Feed>().expect("a feed");
    let mut index = Index::new(s(&data["release_id"]), feed);
    for item in items(data.get("keys")) {
        let r = &item[0];
        let key = index.intern(&KeyRef::new(s(&r[0]), s(&r[1])));
        let entries: Vec<KeyEntry> = items(Some(&item[1]))
            .map(|e| KeyEntry {
                public_id: index.pool.id(s(&e[0])),
                valid_from: date(&e[1]),
                valid_to: date(&e[2]),
            })
            .collect();
        index.key_entries.insert(key, entries.into());
    }
    for item in items(data.get("lines")) {
        let r = &item[0];
        let key = index.intern(&KeyRef::new(s(&r[0]), s(&r[1])));
        let keys: Vec<Arc<str>> = items(Some(&item[1])).map(|k| index.pool.id(s(k))).collect();
        index.lines.insert(key, keys.into());
    }
    for t in items(data.get("trips")) {
        let calls: Vec<Call> = items(Some(&t["calls"]))
            .map(|c| Call {
                sequence: int(&c[0]) as i32,
                location_id: index.pool.id(s(&c[1])),
                visit_n: int(&c[2]) as u32,
                passenger_service: c[3].as_bool().expect("a bool"),
                arrival: c[4].as_i64().map(|v| v as i32),
                departure: c[5].as_i64().map(|v| v as i32),
                on_request: c.get(6).and_then(Value::as_bool).unwrap_or(false),
                point_id: c.get(7).and_then(Value::as_str).map(|p| index.pool.id(p)),
            })
            .collect();
        let pool = &mut index.pool;
        let trip = Trip {
            trip_id: pool.id(s(&t["trip_id"])),
            route_id: pool.id(s(&t["route_id"])),
            route_name: pool.id(s(&t["route_name"])),
            mode: pool.id(s(&t["mode"])),
            service_id: pool.id(s(&t["service_id"])),
            headsign: pool.opt(t["headsign"].as_str()),
            shape_id: pool.opt(t["shape_id"].as_str()),
            calls: calls.into(),
            run_key: pool.opt(t.get("run_key").and_then(Value::as_str)),
            run_part: t.get("run_part").and_then(Value::as_i64).map(|p| p as i16),
            train_number: pool.opt(t.get("train_number").and_then(Value::as_str)),
            parts: Box::new([]),
            agency_id: pool.opt(t.get("agency_id").and_then(Value::as_str)),
        };
        index.trips.insert(trip.trip_id.clone(), Arc::new(trip));
    }
    for item in items(data.get("runs")) {
        let run = index.pool.id(s(&item[0]));
        let parts: Vec<Arc<str>> = items(Some(&item[1])).map(|p| index.pool.id(s(p))).collect();
        index.runs.insert(run, parts.into());
    }
    for item in items(data.get("location_keys")) {
        index.add_location_key(s(&item[0]), s(&item[1]), s(&item[2]));
    }
    if let Some(services) = data["service_dates"].as_object() {
        for (service, days) in services {
            let mut set = DateSet::default();
            for d in items(Some(days)) {
                set.insert(date(d));
            }
            let service = index.pool.id(service);
            index.service_dates.insert(service, set);
        }
    }
    for d in items(data.get("loaded_dates")) {
        index.loaded_dates.insert(date(d));
    }
    for loc in items(data.get("locations")) {
        let id = index.pool.id(s(&loc[0]));
        let location = Location {
            location_id: id.clone(),
            name: index.pool.id(s(&loc[1])),
            lon: loc[2].as_f64(),
            lat: loc[3].as_f64(),
        };
        index.locations.insert(id, location);
    }
    for shape in items(data.get("shapes")) {
        let id = index.pool.id(s(&shape["shape_id"]));
        let points: Vec<(f64, f64)> = items(Some(&shape["points"]))
            .map(|p| (p[0].as_f64().expect("lon"), p[1].as_f64().expect("lat")))
            .collect();
        let distances: Vec<f64> = items(Some(&shape["distances_m"]))
            .map(|d| d.as_f64().expect("metres"))
            .collect();
        index.shapes.insert(
            id.clone(),
            Arc::new(Shape {
                shape_id: id,
                points: points.into(),
                distances_m: distances.into(),
            }),
        );
    }
    let observations = items(data.get("observations")).map(observation).collect();
    (index, observations)
}

/// What a journey's run reports to: each step's and tick's effects, and the state after each
/// tick (for the state rows and GTFS-RT).
pub trait Journal {
    fn stepped(&mut self, phase: &str, at: Instant, effects: &[Effect]) -> io::Result<()>;
    fn tick(&mut self, state: &FeedState, index: &Index, now: Instant) -> io::Result<()>;
}

/// Step one journey through the core with the replay's emit ticks (`corpus.run`).
pub fn run(
    index: &mut Index,
    observations: Vec<Observation>,
    policy: &Policy,
    semantics: &Semantics,
    journal: &mut impl Journal,
) -> io::Result<FeedState> {
    let mut state = FeedState::new(index.feed);
    let mut plans = PlanCache::new();
    let mut progress = SourceProgress::new();
    let mut ordered: Vec<Arc<Observation>> = observations.into_iter().map(Arc::new).collect();
    // Stable: full ties keep the trace's order.
    ordered.sort_by(|a, b| {
        (a.received_at, &a.source, a.raw.item).cmp(&(b.received_at, &b.source, b.raw.item))
    });
    let tick = policy.emit.tick_s;
    let mut now = next_multiple(ordered[0].received_at, tick);
    let end = ordered[ordered.len() - 1].received_at + TimeDelta::from_seconds(AFTER_S);
    let mut pending = ordered.into_iter().peekable();
    let error = |e: obehy_core::core::CoreError| io::Error::other(e.to_string());
    while now <= end {
        let mut ctx = Context {
            index: &mut *index,
            plans: &mut plans,
            progress: &mut progress,
            policy,
            semantics,
        };
        while let Some(upcoming) = pending.next_if(|o| o.received_at <= now) {
            let produced = step(&mut state, &upcoming, &mut ctx).map_err(error)?;
            journal.stepped("step", upcoming.received_at, &produced)?;
        }
        let produced = refresh(&mut state, now, &mut ctx).map_err(|e| error(e.into()))?;
        journal.stepped("tick", now, &produced)?;
        estimate_all(&mut state, &mut ctx, now).map_err(|e| error(e.into()))?;
        journal.tick(&state, index, now)?;
        now = now + TimeDelta::from_seconds(tick);
    }
    Ok(state)
}

/// A journey's stream after its header: effects and rows, state and GTFS-RT every minute.
struct Lines<'a, 'p> {
    emit: Emit<'a>,
    recording: Recording<HarnessBook>,
    policy: &'p Policy,
    last: Option<Instant>,
}

impl Journal for Lines<'_, '_> {
    fn stepped(&mut self, phase: &str, at: Instant, effects: &[Effect]) -> io::Result<()> {
        effect_records(self.emit, at, phase, effects)?;
        self.recording.write(effects)?;
        self.recording.flush(Some(at), self.emit)
    }

    fn tick(&mut self, state: &FeedState, _index: &Index, now: Instant) -> io::Result<()> {
        self.last = Some(now);
        if now.timestamp_int().rem_euclid(STATE_EVERY_S) == 0 {
            self.recording.write_state(&[(state.feed, state)])?;
            self.recording.flush(Some(now), self.emit)?;
            let record = self.recording.gtfs_rt(state, now, &self.policy.lifecycle)?;
            (self.emit)(&record)?;
        }
        Ok(())
    }
}

/// One journey's stream (`corpus_stream.stream`).
pub fn stream(
    name: &str,
    trace: &Path,
    policy: &Policy,
    semantics: &Semantics,
    sources: &[Source],
    emit: Emit,
) -> io::Result<()> {
    let (mut index, observations) = from_json(&read_gz_json(trace)?);
    emit(&C::obj([
        ("kind", C::str("header")),
        ("corpus", C::str(name)),
        ("feed", C::str(index.feed.as_str())),
        ("core_version", C::str(CORE_VERSION)),
        ("policy_version", C::str(&policy.version)),
    ]))?;
    let derivation = Derivation {
        core_version: CORE_VERSION.to_owned(),
        policy_version: policy.version.clone(),
        release_id: "corpus".to_owned(),
    };
    let mut recording = Recording::open(sources, derivation, harness_book()?)?;
    recording.flush(None, emit)?;
    let mut journal = Lines {
        emit,
        recording,
        policy,
        last: None,
    };
    let state = run(&mut index, observations, policy, semantics, &mut journal)?;
    let now = journal.last.expect("a journey has a tick");
    journal.recording.write_state(&[(state.feed, &state)])?;
    journal.recording.flush(Some(now), journal.emit)
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::FAILURE
}

pub fn run_command(args: &CorpusArgs) -> ExitCode {
    let paths = match Paths::discover() {
        Ok(p) => p,
        Err(e) => return fail(e),
    };
    let policy = match paths.load_policy() {
        Ok(p) => p,
        Err(e) => return fail(e),
    };
    let sources = match manifest::load_sources(&paths.sources(), &RECORDER_FILTERS) {
        Ok(s) => s,
        Err(e) => return fail(e),
    };
    // `runner.shipped_semantics`: every shipped channel's.
    let semantics: Semantics = sources
        .iter()
        .flat_map(|s| s.channels.iter())
        .map(|c| ((c.source.clone(), c.channel.clone()), c.core_semantics))
        .collect();
    let corpus = args
        .corpus
        .clone()
        .unwrap_or_else(|| paths.root.join("tests/realtime/corpus"));
    let reference = args
        .reference
        .clone()
        .unwrap_or_else(|| paths.root.join("tests/golden/corpus-stream"));
    let mut names = args.names.clone();
    if names.is_empty() {
        let found = match std::fs::read_dir(&corpus) {
            Ok(found) => found,
            Err(e) => return fail(format!("{}: {e}", corpus.display())),
        };
        names = found
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                e.file_name()
                    .to_str()
                    .and_then(|n| n.strip_suffix(".json.gz"))
                    .map(str::to_owned)
            })
            .collect();
        names.sort();
    }
    let started = std::time::Instant::now();
    let mut failed = 0;
    for name in &names {
        let out = args.out.join(format!("{name}.jsonl.gz"));
        let trace = corpus.join(format!("{name}.json.gz"));
        if let Err(e) = write_stream(&out, |emit| {
            stream(name, &trace, &policy, &semantics, &sources, emit)
        }) {
            return fail(format!("{name}: {e}"));
        }
        if !args.check {
            continue;
        }
        match diff(
            &reference.join(format!("{name}.jsonl.gz")),
            &out,
            args.limit,
            &args.ignore,
        ) {
            Ok((true, _)) => {}
            Ok((false, report)) => {
                failed += 1;
                println!("== {name}\n{}", report.join("\n"));
            }
            Err(e) => return fail(format!("{name}: {e}")),
        }
    }
    println!(
        "{} journeys in {:.1} s",
        names.len(),
        started.elapsed().as_secs_f64()
    );
    if args.check {
        println!(
            "{}/{} journeys give their pinned stream",
            names.len() - failed,
            names.len()
        );
    }
    if failed > 0 {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}
