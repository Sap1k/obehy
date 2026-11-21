//! The Rust writer against Python's (R7): drives [`Writer`] (with the core's vehicle book and a
//! [`RecordingStore`]) with the effects an output stream recorded (`obehy rt compare --level
//! output`), and compares the rows it makes with the rows Python's writer made of the same
//! effects, record for record. The state tables need the core's state, which the stream does not
//! hold: at each state write the vehicles its rows name are identified as `write_state` does
//! and the book's rows compared; the state rows themselves are left to the output harness.
//!
//! ```text
//! cargo run --release -p obehy-rt --example writer_parity -- --stream S [--limit N]
//! ```

use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use obehy_core::config::manifest;
use obehy_core::model::{Derivation, Effect, Observation};
use obehy_core::time::{Instant, local_date};
use obehy_core::vehicles::Vocabulary;
use obehy_core::vehicles::vocabulary::VOCABULARY_FILE;
use obehy_rt::canonical::{C, line};
use obehy_rt::settings::Paths;
use obehy_rt::store::records::{effect, observation_key, observation_of};
use obehy_rt::store::vehicles::{Book, book, physical_rows};
use obehy_rt::store::{RecordingStore, Store, Writer};
use serde_json::Value;

fn open(path: &PathBuf) -> std::io::Result<Box<dyn BufRead>> {
    let file = File::open(path)?;
    Ok(if path.extension().is_some_and(|e| e == "gz") {
        Box::new(BufReader::new(flate2::read::MultiGzDecoder::new(file)))
    } else {
        Box::new(BufReader::new(file))
    })
}

/// A record's canonical text without its `at`.
fn text(record: &Value) -> String {
    let mut record = record.clone();
    if let Value::Object(map) = &mut record {
        map.remove("at");
    }
    line(&C::from_json(&record))
}

#[derive(Default)]
struct Tally {
    compared: BTreeMap<String, usize>,
    differing: usize,
    /// Differing writes that are effect writes (not state writes).
    differing_effects: usize,
    shown: usize,
}

impl Tally {
    fn compare(&mut self, what: &str, expected: &[Value], produced: &[C]) {
        let expected: Vec<String> = expected.iter().map(text).collect();
        let produced: Vec<String> = produced.iter().map(line).collect();
        for record in &expected {
            let v: Value = serde_json::from_str(record).unwrap_or_default();
            let kind = v["kind"].as_str().unwrap_or("?").to_owned();
            *self.compared.entry(kind).or_default() += 1;
        }
        if expected != produced {
            self.differing += 1;
            if what != "state write" {
                self.differing_effects += 1;
            }
            if self.shown < 3 && what != "state write" {
                self.shown += 1;
                println!(
                    "-- {what}: {} expected, {} produced",
                    expected.len(),
                    produced.len()
                );
                for (i, (e, p)) in expected.iter().zip(&produced).enumerate() {
                    if e != p {
                        println!("first difference at {i}:\n  python {}  rust   {}", e, p);
                        break;
                    }
                }
            }
        }
    }
}

type Rust = Writer<RecordingStore, obehy_core::vehicles::VehicleBook>;

fn effects_segment(
    writer: &mut Rust,
    tally: &mut Tally,
    effects: &[Value],
    rows: &[Value],
) -> Result<(), Box<dyn std::error::Error>> {
    let mut observations: HashMap<(String, String, i64, String), Arc<Observation>> = HashMap::new();
    for r in rows.iter().filter(|r| r["kind"] == "observation") {
        observations.insert(
            observation_key(&r["row"]),
            Arc::new(observation_of(&r["row"])?),
        );
    }
    let parsed: Vec<Effect> = effects
        .iter()
        .map(|e| effect(e, &observations))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .flatten()
        .collect();
    writer.write(&parsed)?;
    let produced = writer.store.take();
    let at = effects
        .first()
        .or(rows.first())
        .map(|r| r["at"].to_string());
    tally.compare(
        &format!("write at {}", at.unwrap_or_default()),
        rows,
        &produced,
    );
    Ok(())
}

fn state_segment(
    writer: &mut Rust,
    tally: &mut Tally,
    records: &[Value],
) -> Result<(), Box<dyn std::error::Error>> {
    for r in records {
        if r["kind"] == "state"
            && r["table"] == "vehicle_state_current"
            && (r["op"] == "insert" || r["op"] == "upsert")
        {
            let row = &r["row"];
            let report = (
                row["source"].as_str().unwrap_or_default().to_owned(),
                row["source_vehicle_id"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
            );
            let seen = row["last_seen"]
                .as_str()
                .unwrap_or_default()
                .replace('Z', "+00:00");
            Book::identify(
                &mut writer.vehicles,
                &report,
                local_date(Instant::from_isoformat(&seen)?),
            );
        }
    }
    let rows = Book::rows(&mut writer.vehicles, Vec::new());
    writer.store.vehicles(&rows)?;
    let produced = writer.store.take();
    let expected: Vec<Value> = records
        .iter()
        .filter(|r| r["kind"] != "state")
        .cloned()
        .collect();
    tally.compare("state write", &expected, &produced);
    Ok(())
}

fn run() -> Result<bool, Box<dyn std::error::Error>> {
    let mut stream = None;
    let mut limit = usize::MAX;
    let mut items = std::env::args().skip(1);
    while let Some(flag) = items.next() {
        let value = items.next().ok_or(format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--stream" => stream = Some(PathBuf::from(value)),
            "--limit" => limit = value.parse()?,
            _ => return Err(format!("unknown flag {flag}").into()),
        }
    }
    let stream = stream.ok_or("--stream is required")?;
    let paths = Paths::discover()?;
    let sources = manifest::load_sources(&paths.sources(), &manifest::RECORDER_FILTERS)?;
    let vocabulary = Vocabulary::load(&paths.data().join("..").join(VOCABULARY_FILE))?;
    let mut lines = open(&stream)?.lines();
    let header: Value = serde_json::from_str(&lines.next().ok_or("empty stream")??)?;
    let text_of = |key: &str| header[key].as_str().unwrap_or_default().to_owned();
    let derivation = Derivation {
        core_version: text_of("core_version"),
        policy_version: text_of("policy_version"),
        release_id: text_of("release"),
    };
    let mut writer: Rust = Writer::open(
        RecordingStore::new(physical_rows(&sources)),
        derivation,
        |stored| book(stored, vocabulary),
    )?;
    let mut tally = Tally::default();
    let opening = writer.store.take();
    let mut effects: Vec<Value> = Vec::new();
    let mut rows: Vec<Value> = Vec::new();
    let mut state: Vec<Value> = Vec::new();
    let mut opened = false;
    let mut count = 0;
    for line in lines {
        let record: Value = serde_json::from_str(&line?)?;
        count += 1;
        if count > limit {
            break;
        }
        let kind = record["kind"].as_str().unwrap_or_default().to_owned();
        let is_state = kind == "state";
        if kind == "physical_vehicles" {
            rows.push(record);
            continue;
        }
        if !opened {
            tally.compare("open", &rows, &opening);
            rows.clear();
            opened = true;
        }
        let boundary = matches!(kind.as_str(), "gtfs_rt" | "poll" | "predictor_sample");
        let starts_effects = kind == "effect" && (!rows.is_empty() || !state.is_empty());
        let starts_state =
            is_state && state.is_empty() && (!effects.is_empty() || !rows.is_empty());
        if boundary || starts_effects || starts_state {
            if !state.is_empty() {
                state_segment(&mut writer, &mut tally, &state)?;
                state.clear();
            } else if !effects.is_empty() || !rows.is_empty() {
                effects_segment(&mut writer, &mut tally, &effects, &rows)?;
                effects.clear();
                rows.clear();
            }
        }
        match kind.as_str() {
            "effect" => effects.push(record),
            "gtfs_rt" | "poll" | "predictor_sample" => {}
            _ if is_state || !state.is_empty() => state.push(record),
            _ => rows.push(record),
        }
    }
    if !state.is_empty() {
        state_segment(&mut writer, &mut tally, &state)?;
    } else if !effects.is_empty() || !rows.is_empty() {
        effects_segment(&mut writer, &mut tally, &effects, &rows)?;
    }
    let compared: Vec<String> = tally
        .compared
        .iter()
        .map(|(k, n)| format!("{k} {n}"))
        .collect();
    println!(
        "{} differing writes ({} of them effect writes); compared: {}",
        tally.differing,
        tally.differing_effects,
        compared.join(", ")
    );
    Ok(tally.differing == 0)
}

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("writer_parity: {error}");
            ExitCode::FAILURE
        }
    }
}
