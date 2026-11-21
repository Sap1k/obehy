//! Křovák (SZ-Q1) against pyproj (PROJ 9.8.1): the unrounded output at 17 significant digits
//! and the 6-decimal values the facts carry, over points on both sides of each operation's box
//! edge and a sample of a day's distinct SŽ positions (tests/fixtures/krovak.json). With
//! data/compare-r2/krovak-day.json present (rust/tools/gen_sources_fixtures.py writes it,
//! gitignored), every distinct position of 2026-10-12 is checked too.

use std::path::PathBuf;

use obehy_core::py::num::py_round_digits;
use obehy_sources::krovak::Transformer;
use serde_json::Value;

struct Outcome {
    points: usize,
    exact: usize,
    max_ulps: u64,
    flipped: Vec<String>,
}

fn ulps(a: f64, b: f64) -> u64 {
    (a.to_bits() as i64 - b.to_bits() as i64).unsigned_abs()
}

fn check(rows: &Value) -> Outcome {
    let t = Transformer::new();
    let mut out = Outcome {
        points: 0,
        exact: 0,
        max_ulps: 0,
        flipped: Vec::new(),
    };
    for row in rows.as_array().expect("rows") {
        let f = |i: usize| -> f64 { row[i].as_str().expect("repr").parse().expect("a float") };
        let (x, y, lon, lat) = (f(0), f(1), f(2), f(3));
        let (got_lon, got_lat) = t.transform(x, y);
        out.points += 1;
        let worst = ulps(got_lon, lon).max(ulps(got_lat, lat));
        out.max_ulps = out.max_ulps.max(worst);
        if worst == 0 {
            out.exact += 1;
        }
        if py_round_digits(got_lat, 6) != py_round_digits(lat, 6)
            || py_round_digits(got_lon, 6) != py_round_digits(lon, 6)
        {
            out.flipped.push(format!(
                "({x}, {y}): pyproj {lon} {lat}, here {got_lon} {got_lat}"
            ));
        }
    }
    out
}

#[test]
fn krovak_matches_pyproj_at_edges_and_on_a_day_sample() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/krovak.json");
    let rows: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let out = check(&rows);
    println!(
        "{} points: {} bit-identical, at most {} ulps apart, {} rounded values flip",
        out.points,
        out.exact,
        out.max_ulps,
        out.flipped.len()
    );
    assert!(out.flipped.is_empty(), "{}", out.flipped.join("\n"));
    assert!(out.max_ulps <= 64, "{} ulps apart", out.max_ulps);
}

#[test]
fn krovak_matches_pyproj_on_every_position_of_a_day() {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data/compare-r2/krovak-day.json");
    let Ok(text) = std::fs::read_to_string(&path) else {
        println!("skipped: no {}", path.display());
        return;
    };
    let out = check(&serde_json::from_str(&text).unwrap());
    println!(
        "{} points: {} bit-identical, at most {} ulps apart, {} rounded values flip",
        out.points,
        out.exact,
        out.max_ulps,
        out.flipped.len()
    );
    assert!(out.flipped.is_empty(), "{}", out.flipped.join("\n"));
}
