//! The index rows read from a release's serving files equal those read from PostgreSQL after
//! `obehy release load`: `tests/db/test_index_parity.py` for the Rust `ServingRows` and
//! `SqlRows`. Every `Rows` method is asked the same question both ways, over every identifier
//! the release holds; the order is compared where the `Rows` trait promises one.
//!
//! ```text
//! cargo run -p obehy-rt --example index_parity -- --release DIR --feed jdf \
//!     --sql-env VAR --load-id N
//! ```

use std::collections::BTreeSet;
use std::fmt::Debug;
use std::path::PathBuf;
use std::process::ExitCode;

use obehy_core::index::{Rows, RowsError};
use obehy_core::model::Feed;
use obehy_core::time::Date;
use obehy_rt::index::serving::Strings;
use obehy_rt::index::{ServingRows, SqlRows, parquet, serving_dir};

fn same<T: Debug>(name: &str, ordered: bool, sql: Vec<T>, files: Vec<T>) -> bool {
    let (mut left, mut right): (Vec<String>, Vec<String>) = (
        sql.iter().map(|r| format!("{r:?}")).collect(),
        files.iter().map(|r| format!("{r:?}")).collect(),
    );
    if !ordered {
        left.sort();
        right.sort();
    }
    let equal = left == right;
    println!(
        "{name}: {} ({} rows)",
        if equal { "same" } else { "DIFFERENT" },
        left.len()
    );
    if !equal {
        for (a, b) in left.iter().zip(&right).filter(|(a, b)| a != b).take(3) {
            println!("  sql   {a}\n  files {b}");
        }
    }
    equal
}

fn r(x: &[String]) -> Vec<&str> {
    x.iter().map(String::as_str).collect()
}

fn run() -> Result<bool, RowsError> {
    let mut items = std::env::args().skip(1);
    let (mut release, mut feed, mut variable, mut load_id) = (None, None, None, None);
    while let Some(flag) = items.next() {
        let value = items.next().ok_or("a flag needs a value")?;
        match flag.as_str() {
            "--release" => release = Some(PathBuf::from(value)),
            "--feed" => feed = Some(value.parse::<Feed>()?),
            "--sql-env" => variable = Some(value),
            "--load-id" => load_id = Some(value.parse::<i32>()?),
            _ => return Err(format!("unknown flag {flag}").into()),
        }
    }
    let (release, feed) = (release.ok_or("--release")?, feed.ok_or("--feed")?);
    let url = std::env::var(variable.ok_or("--sql-env")?)?;
    let serving = serving_dir(&release, feed);
    let mut sql = SqlRows::connect(&url, load_id.ok_or("--load-id")?)?;
    let mut files = ServingRows::open(&serving)?;

    // Every identifier the release holds.
    let mut s = Strings::default();
    let keys = parquet::source_keys(&serving.join("source_key.parquet"), &mut s)?;
    let trips = parquet::trips(&serving.join("trip.parquet"), &mut s)?;
    let locations = parquet::locations(&serving.join("location.parquet"), &mut s)?;
    let points = parquet::shape_points(&serving.join("shape_point.parquet"), &mut s)?;
    let texts = |ids: &[u32]| -> Vec<String> {
        let set: BTreeSet<&str> = ids.iter().filter_map(|i| s.opt(*i)).collect();
        set.into_iter().map(str::to_owned).collect()
    };
    let pairs: BTreeSet<(&str, &str)> = keys
        .namespace
        .iter()
        .zip(&keys.identifier)
        .map(|(n, i)| (s.text(*n), s.text(*i)))
        .collect();
    let namespaces: Vec<&str> = pairs.iter().map(|p| p.0).collect();
    let identifiers: Vec<&str> = pairs.iter().map(|p| p.1).collect();
    let (trip_ids, services, run_keys) = (
        texts(&trips.trip_id),
        texts(&trips.service_id),
        texts(&trips.run_key),
    );
    let (location_ids, shape_ids) = (texts(&locations.location_id), texts(&points.shape_id));
    let lines: BTreeSet<&str> = identifiers
        .iter()
        .map(|i| i.split_once(':').map_or(*i, |(l, _)| l))
        .collect();
    let lines: Vec<&str> = lines.into_iter().collect();
    let codes: Vec<&str> = pairs
        .iter()
        .filter(|p| p.0 == "sr70")
        .map(|p| p.1)
        .collect();
    let days: Vec<Date> = (0..16)
        .map(|n| Date::ymd(2026, 10, 1).add_days(n))
        .collect();
    if trip_ids.is_empty() {
        return Err("the feed has no trips".into());
    }
    let mut ok = true;
    ok &= same(
        "keys",
        true,
        sql.keys(&namespaces, &identifiers)?,
        files.keys(&namespaces, &identifiers)?,
    );
    let distinct: BTreeSet<&str> = namespaces.iter().copied().collect();
    for namespace in distinct {
        ok &= same(
            &format!("line_keys {namespace}"),
            false,
            sql.line_keys(namespace, &lines)?,
            files.line_keys(namespace, &lines)?,
        );
    }
    ok &= same(
        "trips",
        false,
        sql.trips(&r(&trip_ids))?,
        files.trips(&r(&trip_ids))?,
    );
    ok &= same(
        "calls",
        true,
        sql.calls(&r(&trip_ids))?,
        files.calls(&r(&trip_ids))?,
    );
    ok &= same(
        "run_parts",
        true,
        sql.run_parts(&r(&run_keys))?,
        files.run_parts(&r(&run_keys))?,
    );
    ok &= same(
        "station_keys",
        true,
        sql.station_keys(&r(&location_ids))?,
        files.station_keys(&r(&location_ids))?,
    );
    ok &= same(
        "track_keys",
        true,
        sql.track_keys(&codes)?,
        files.track_keys(&codes)?,
    );
    ok &= same(
        "locations",
        false,
        sql.locations(&r(&location_ids))?,
        files.locations(&r(&location_ids))?,
    );
    ok &= same(
        "shape_points",
        true,
        sql.shape_points(&r(&shape_ids))?,
        files.shape_points(&r(&shape_ids))?,
    );
    ok &= same(
        "service_dates",
        false,
        sql.service_dates(&r(&services), &days)?,
        files.service_dates(&r(&services), &days)?,
    );
    Ok(ok)
}

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("index_parity: {error}");
            ExitCode::FAILURE
        }
    }
}
