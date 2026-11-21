//! The Rust index against the Python one (R3): runs a load plan written by
//! `rust/tools/index_crosscheck.py` with the Parquet or the PostgreSQL rows, and compares the
//! index at each `dump` step with Python's dump. Also reports load time and the index's heap.
//!
//! ```text
//! cargo run --release -p obehy-rt --example index_check -- --plan P --expect E
//!     [--sql-env VAR --load-id N --release-id R]
//! ```
//!
//! With `--sql-env`, the database URL is read from the environment variable `VAR`.

use std::alloc::System;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant as Clock;

use obehy_core::index::dump::{counts, dump};
use obehy_core::index::{Index, IndexLoader, KeyRef, Rows, RowsError};
use obehy_core::model::Feed;
use obehy_core::py::json::{DumpOptions, PyJson, dumps};
use obehy_core::time::{Date, Instant};
use obehy_rt::index::{ServingRows, SqlRows, release_run_id, serving_dir};
use serde_json::Value;
use stats_alloc::{INSTRUMENTED_SYSTEM, Region, StatsAlloc};

#[global_allocator]
static GLOBAL: &StatsAlloc<System> = &INSTRUMENTED_SYSTEM;

struct Args {
    plan: PathBuf,
    expect: PathBuf,
    sql_env: Option<String>,
    load_id: Option<i32>,
    release_id: Option<String>,
}

fn args() -> Result<Args, RowsError> {
    let mut found = Args {
        plan: PathBuf::new(),
        expect: PathBuf::new(),
        sql_env: None,
        load_id: None,
        release_id: None,
    };
    let mut items = std::env::args().skip(1);
    while let Some(flag) = items.next() {
        let value = items
            .next()
            .ok_or_else(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--plan" => found.plan = value.into(),
            "--expect" => found.expect = value.into(),
            "--sql-env" => found.sql_env = Some(value),
            "--load-id" => found.load_id = Some(value.parse()?),
            "--release-id" => found.release_id = Some(value),
            _ => return Err(format!("unknown flag {flag}").into()),
        }
    }
    Ok(found)
}

fn text(value: &Value) -> &str {
    value.as_str().expect("a string")
}

fn keys(value: &Value) -> Vec<KeyRef> {
    value
        .as_array()
        .expect("keys")
        .iter()
        .map(|pair| KeyRef::new(text(&pair[0]), text(&pair[1])))
        .collect()
}

fn days(value: &Value) -> Vec<Date> {
    value
        .as_array()
        .expect("dates")
        .iter()
        .map(|d| Date::from_isoformat(text(d)).expect("a date"))
        .collect()
}

fn instant(value: &Value) -> Instant {
    Instant::from_isoformat(text(value)).expect("an instant")
}

/// Run the plan's steps; the dumps it asks for (name, counts, index) and the load seconds.
fn execute<R: Rows>(
    loader: &mut IndexLoader<R>,
    steps: &[Value],
    dumping: bool,
) -> Result<(Vec<(String, PyJson)>, f64), RowsError> {
    let mut dumps = Vec::new();
    let mut spent = 0.0;
    for step in steps {
        let started = Clock::now();
        match text(&step["op"]) {
            "ensure" => {
                let (refs, lines) = (keys(&step["refs"]), keys(&step["lines"]));
                loader.ensure(&refs, days(&step["dates"]), &lines)?;
            }
            "touch" => {
                let (refs, lines) = (keys(&step["refs"]), keys(&step["lines"]));
                loader.touch(&refs, &lines, instant(&step["at"]));
            }
            "run" => {
                for pair in step["trips"].as_array().expect("trips") {
                    let trip = loader.index.trip(text(&pair[0]))?.clone();
                    let day = Date::from_isoformat(text(&pair[1]))?;
                    loader.index.run(&trip, day)?;
                }
            }
            "sweep" => {
                let trip_ids: Vec<&str> = step["trip_ids"]
                    .as_array()
                    .expect("trip ids")
                    .iter()
                    .map(text)
                    .collect();
                let refs = keys(&step["refs"]);
                let day = Date::from_isoformat(text(&step["day"]))?;
                loader.sweep(instant(&step["since"]), trip_ids, &refs, day);
            }
            "dump" => {
                if dumping {
                    let index = &loader.index;
                    dumps.push((
                        text(&step["name"]).to_owned(),
                        PyJson::object([("counts", counts(index)), ("index", dump(index))]),
                    ));
                }
                continue;
            }
            other => return Err(format!("unknown step {other}").into()),
        }
        spent += started.elapsed().as_secs_f64();
    }
    Ok((dumps, spent))
}

/// The first place two JSON values differ, as a path.
fn difference(path: &str, a: &Value, b: &Value) -> Option<String> {
    match (a, b) {
        (Value::Array(x), Value::Array(y)) => {
            for (i, (p, q)) in x.iter().zip(y).enumerate() {
                if let Some(found) = difference(&format!("{path}[{i}]"), p, q) {
                    return Some(found);
                }
            }
            (x.len() != y.len()).then(|| format!("{path}: {} vs {} items", x.len(), y.len()))
        }
        (Value::Object(x), Value::Object(y)) => {
            for (key, p) in x {
                match y.get(key) {
                    Some(q) => {
                        if let Some(found) = difference(&format!("{path}.{key}"), p, q) {
                            return Some(found);
                        }
                    }
                    None => return Some(format!("{path}.{key}: missing in Python")),
                }
            }
            y.keys()
                .find(|k| !x.contains_key(*k))
                .map(|k| format!("{path}.{k}: missing in Rust"))
        }
        _ => (a != b).then(|| format!("{path}: rust {a} vs python {b}")),
    }
}

fn live_bytes(region: &Region<System>) -> isize {
    let change = region.change();
    // Reallocations are already in the allocated and deallocated bytes.
    change.bytes_allocated as isize - change.bytes_deallocated as isize
}

fn check<R: Rows>(
    make: &mut dyn FnMut() -> Result<IndexLoader<R>, RowsError>,
    after: &dyn Fn(&R),
    plan: &Value,
    expect: &Value,
) -> Result<bool, RowsError> {
    let steps = plan["steps"].as_array().expect("steps");
    let without_dumps: Vec<Value> = steps
        .iter()
        .filter(|s| s["op"] != "dump")
        .cloned()
        .collect();
    let mut loader = make()?;
    let region = Region::new(GLOBAL);
    let (_, spent) = execute(&mut loader, &without_dumps, false)?;
    let heap = live_bytes(&region);
    after(&loader.rows);
    let loaded = counts(&loader.index);
    let (used, pool) = (loader.used.len(), loader.index.pool.len());
    let index = std::mem::replace(&mut loader.index, Index::new("", Feed::Jdf));
    let before = live_bytes(&region);
    drop(index);
    let own = before - live_bytes(&region);
    drop(loader);
    println!(
        "rust:   the index itself {:.1} MB ({pool} interned IDs, {used} keys in use)",
        own as f64 / 1e6
    );
    let python = &expect["python"];
    println!(
        "rust:   load {spent:.3} s, index heap {:.1} MB, {}",
        heap as f64 / 1e6,
        dumps(&loaded, DumpOptions::DEFAULT)
    );
    println!(
        "python: load {} s, index heap {:.1} MB, {}",
        python["load_s"],
        python["heap_bytes_after"].as_f64().unwrap_or(0.0) / 1e6,
        python["counts"]
    );
    let mut loader = make()?;
    let (found, _) = execute(&mut loader, steps, true)?;
    let wanted = expect["dumps"].as_array().expect("dumps");
    if found.len() != wanted.len() {
        println!("{} dumps, Python has {}", found.len(), wanted.len());
        return Ok(false);
    }
    let options = DumpOptions {
        ensure_ascii: false,
        ..DumpOptions::DEFAULT
    };
    let mut same = true;
    for ((name, mine), theirs) in found.iter().zip(wanted) {
        let mine: Value = serde_json::from_str(&dumps(mine, options))?;
        let theirs = serde_json::json!({"counts": theirs["counts"], "index": theirs["index"]});
        match difference("", &mine, &theirs) {
            None => println!("{name}: identical ({})", mine["counts"]),
            Some(found) => {
                println!("{name}: DIFFERENT at {found}");
                same = false;
            }
        }
    }
    Ok(same)
}

fn run() -> Result<bool, RowsError> {
    let args = args()?;
    let plan: Value = serde_json::from_str(&std::fs::read_to_string(&args.plan)?)?;
    let expect: Value = serde_json::from_str(&std::fs::read_to_string(&args.expect)?)?;
    let feed: Feed = text(&plan["feed"]).parse()?;
    let release = PathBuf::from(text(&plan["release"]));
    println!(
        "plan {}: {} steps",
        text(&plan["name"]),
        plan["steps"].as_array().map_or(0, Vec::len)
    );
    match &args.sql_env {
        Some(variable) => {
            let url = std::env::var(variable).map_err(|_| format!("{variable} is not set"))?;
            let load_id = args.load_id.ok_or("--load-id is needed with --sql-env")?;
            let release_id = match &args.release_id {
                Some(id) => id.clone(),
                None => release_run_id(&release)?,
            };
            println!("rows: PostgreSQL, load {load_id}");
            check(
                &mut || {
                    let rows = SqlRows::connect(&url, load_id)?;
                    Ok(IndexLoader::new(rows, release_id.clone(), feed))
                },
                &|rows: &SqlRows| {
                    let (count, waited) = rows.queries();
                    let ping = Clock::now();
                    for _ in 0..20 {
                        rows.block_on(rows.client().simple_query("SELECT 1"))
                            .expect("SELECT 1");
                    }
                    println!(
                        "rust:   {count} queries, {waited:.3} s waiting for them; a round trip                          takes {:.1} ms",
                        ping.elapsed().as_secs_f64() * 1000.0 / 20.0
                    );
                },
                &plan,
                &expect,
            )
        }
        None => {
            let release_id = release_run_id(&release)?;
            let started = Clock::now();
            let mut rows = ServingRows::open(&serving_dir(&release, feed))?;
            rows.read_all()?;
            println!(
                "rows: serving Parquet, tables read in {:.2} s",
                started.elapsed().as_secs_f64()
            );
            // The first loader (timed) gets the tables read above; the second reads its own.
            let mut first = Some(rows);
            check(
                &mut || {
                    let rows = first
                        .take()
                        .map_or_else(|| ServingRows::open(&serving_dir(&release, feed)), Ok)?;
                    Ok(IndexLoader::new(rows, release_id.clone(), feed))
                },
                &|_: &ServingRows| {},
                &plan,
                &expect,
            )
        }
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("index_check: {error}");
            ExitCode::FAILURE
        }
    }
}
