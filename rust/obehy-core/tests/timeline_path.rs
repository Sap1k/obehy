//! `timeline/path.rs` against Python (`tools/gen_timeline_fixtures.py`): real trips of a release
//! built into paths bit for bit (GEOS Douglas-Peucker included), projections, points and
//! headings around them; the platform libm against every argument Python passed it; and the
//! unit test of `tests/unit/realtime/test_path.py`.

mod common;

use std::path::PathBuf;

use obehy_core::config::Policy;
use obehy_core::config::policy::{POLICY_FILE, ProgressPolicy};
use obehy_core::geo::{LonLat, cumulative_m};
use obehy_core::py::num::{py_hypot, py_max, py_min};
use obehy_core::timeline::path::{
    Path, build_path, candidates, douglas_peucker, heading_at, point_at, project_all, simplify,
};
use serde_json::Value;

fn policy() -> Policy {
    let path: PathBuf = common::repo()
        .join("src/obehy/data/realtime")
        .join(POLICY_FILE);
    Policy::load(&path).unwrap()
}

/// FNV-1a over the IEEE bits of each value, as the generator folds them.
fn fnv(values: impl IntoIterator<Item = f64>) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for v in values {
        h ^= v.to_bits();
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{h:016x}")
}

fn f(v: &Value) -> f64 {
    v.as_f64().unwrap_or_else(|| panic!("a float: {v}"))
}

fn pair(v: &Value) -> LonLat {
    (f(&v[0]), f(&v[1]))
}

fn pairs(v: &Value) -> Vec<LonLat> {
    v.as_array().unwrap().iter().map(pair).collect()
}

fn same(actual: f64, expected: f64, what: &str) {
    assert_eq!(
        actual.to_bits(),
        expected.to_bits(),
        "{what}: {actual:?} != {expected:?}"
    );
}

/// `progress._sigma`: GPS error combined with the trust of the geometry.
fn sigma(policy: &ProgressPolicy) -> impl Fn(&Path, f64) -> f64 + '_ {
    move |path: &Path, segment_m: f64| {
        let geometry = if path.shaped {
            policy.shape_sigma_m
        } else {
            let chord = py_max(policy.chord_min_sigma_m, policy.chord_k * segment_m);
            py_min(policy.chord_max_sigma_m, chord)
        };
        py_hypot(&[policy.gps_sigma_m, geometry])
    }
}

fn check_candidates(found: &[obehy_core::timeline::path::Candidate], expected: &Value, what: &str) {
    let expected = expected.as_array().unwrap();
    assert_eq!(found.len(), expected.len(), "{what}: count");
    for (c, e) in found.iter().zip(expected) {
        same(c.along_m, f(&e[0]), what);
        same(c.lateral_m, f(&e[1]), what);
        same(c.sigma_m, f(&e[2]), what);
        same(c.heading_deg, common::float(&e[3]), what);
    }
}

fn check_queries(path: &Path, queries: &Value, policy: &ProgressPolicy, what: &str) {
    let sigma = sigma(policy);
    for (q, query) in queries.as_array().unwrap().iter().enumerate() {
        let what = format!("{what} query {q}");
        if let Some(alongs) = query.get("alongs") {
            for (i, along) in alongs.as_array().unwrap().iter().enumerate() {
                let along = f(along);
                let (lon, lat) = point_at(path, along);
                let expected = pair(&query["points"][i]);
                same(lon, expected.0, &format!("{what} point_at({along}) lon"));
                same(lat, expected.1, &format!("{what} point_at({along}) lat"));
                same(
                    heading_at(path, along),
                    common::float(&query["headings"][i]),
                    &format!("{what} heading_at({along})"),
                );
            }
            continue;
        }
        let (lon, lat) = (f(&query["lon"]), f(&query["lat"]));
        let projections = project_all(path, lon, lat);
        assert_eq!(
            fnv(projections
                .iter()
                .flat_map(|p| [p.along_m, p.lateral_m, p.segment_m])),
            query["project_all"].as_str().unwrap(),
            "{what}: project_all"
        );
        // min(key=lateral_m): the first of equal values.
        let mut nearest = projections[0];
        for p in &projections[1..] {
            if p.lateral_m < nearest.lateral_m {
                nearest = *p;
            }
        }
        same(nearest.along_m, f(&query["nearest"][0]), &what);
        same(nearest.lateral_m, f(&query["nearest"][1]), &what);
        same(nearest.segment_m, f(&query["nearest"][2]), &what);
        let found = candidates(path, lon, lat, &sigma, policy.reach_sigmas, None);
        check_candidates(&found, &query["candidates"], &format!("{what} candidates"));
        let window = pair(&query["window"]);
        let found = candidates(path, lon, lat, &sigma, policy.reach_sigmas, Some(window));
        check_candidates(&found, &query["windowed"], &format!("{what} windowed"));
    }
}

#[test]
fn real_trips_build_the_same_paths_as_python() {
    let fixture = common::json("path.json");
    let policy = policy();
    let progress = &policy.progress;
    assert_eq!(fixture["geos"], "3.13.1", "the GEOS the port follows");
    same(
        progress.shape_simplify_deg,
        f(&fixture["policy"]["shape_simplify_deg"]),
        "policy",
    );
    same(
        progress.stop_off_shape_m,
        f(&fixture["policy"]["stop_off_shape_m"]),
        "policy",
    );
    same(
        progress.reach_sigmas,
        f(&fixture["policy"]["reach_sigmas"]),
        "policy",
    );

    let shapes = fixture["shapes"].as_object().unwrap();
    let mut dp_runs = 0;
    for (shape_id, shape) in shapes {
        let points = pairs(&shape["points"]);
        let distances = cumulative_m(&points);
        assert_eq!(
            fnv(distances.iter().copied()),
            shape["distances"].as_str().unwrap(),
            "shape {shape_id}: cumulative_m"
        );
        for dp in shape["dp"].as_array().unwrap() {
            let kept = douglas_peucker(&points, f(&dp["tolerance"]));
            assert_eq!(
                kept.len() as u64,
                dp["n"].as_u64().unwrap(),
                "shape {shape_id}: DP {dp}"
            );
            assert_eq!(
                fnv(kept.iter().flat_map(|p| [p.0, p.1])),
                dp["kept"].as_str().unwrap(),
                "shape {shape_id}: DP {}",
                dp["tolerance"]
            );
            dp_runs += 1;
        }
    }

    let trips = fixture["trips"].as_array().unwrap();
    let mut queries = 0;
    for trip in trips {
        let trip_id = trip["trip_id"].as_str().unwrap();
        let located: Vec<Option<LonLat>> = trip["located"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| (!v.is_null()).then(|| pair(v)))
            .collect();
        let times: Vec<Option<i64>> = trip["times"]
            .as_array()
            .unwrap()
            .iter()
            .map(Value::as_i64)
            .collect();
        let shape = &shapes[trip["shape_id"].as_str().unwrap()];
        let points = pairs(&shape["points"]);
        let distances = cumulative_m(&points);
        for variant in ["shaped", "straight"] {
            let what = format!("{trip_id} {variant}");
            let expected = &trip[variant];
            let given = (variant == "shaped").then_some((&points[..], &distances[..]));
            let built = build_path(&located, given, |i| times[i].ok_or(i), progress);
            if expected.get("error").is_some() {
                assert!(built.is_err(), "{what}: Python raised IndexMiss");
                continue;
            }
            let path = built.unwrap_or_else(|i| panic!("{what}: call {i} has no time"));
            assert_eq!(
                path.points.len() as u64,
                expected["n"].as_u64().unwrap(),
                "{what}"
            );
            assert_eq!(path.shaped, expected["shaped"].as_bool().unwrap(), "{what}");
            assert_eq!(
                fnv(path.points.iter().flat_map(|p| [p.0, p.1])),
                expected["points"].as_str().unwrap(),
                "{what}: points"
            );
            assert_eq!(
                fnv(path.distances_m.iter().copied()),
                expected["distances"].as_str().unwrap(),
                "{what}: distances"
            );
            assert_eq!(
                fnv(path.segments.iter().flat_map(|s| [s.0, s.1, s.2, s.3])),
                expected["segments"].as_str().unwrap(),
                "{what}: segments"
            );
            let calls = expected["call_distances"].as_array().unwrap();
            assert_eq!(path.call_distances_m.len(), calls.len(), "{what}");
            for (i, (actual, e)) in path.call_distances_m.iter().zip(calls).enumerate() {
                same(*actual, f(e), &format!("{what}: call {i}"));
            }
            check_queries(&path, &expected["queries"], progress, &what);
            queries += expected["queries"].as_array().unwrap().len();
        }
    }
    assert!(shapes.len() >= 100 && trips.len() >= 100, "a real sample");
    assert!(dp_runs >= 400 && queries >= 1000, "{dp_runs} {queries}");
}

#[test]
fn geos_douglas_peucker_corner_cases() {
    let fixture = common::json("path.json");
    for case in fixture["dp_cases"].as_array().unwrap() {
        let points = pairs(&case["points"]);
        let tolerance = f(&case["tolerance"]);
        assert_eq!(
            douglas_peucker(&points, tolerance),
            pairs(&case["kept"]),
            "{case}"
        );
        let distances: Vec<f64> = (0..points.len()).map(|i| i as f64).collect();
        let (kept, at) = simplify(&points, &distances, tolerance);
        assert_eq!(kept, pairs(&case["simplified"]["points"]), "{case}");
        let expected: Vec<f64> = case["simplified"]["distances"]
            .as_array()
            .unwrap()
            .iter()
            .map(f)
            .collect();
        assert_eq!(at, expected, "{case}");
    }
}

/// The platform libm (Rust's f64 methods) against Python's `math` on every distinct argument
/// path.py, geo.py, progress.py and runtimes.py passed while the fixture was made (sampled);
/// `hypot` is CPython's own (`py_hypot`).
#[test]
fn libm_matches_python_on_the_arguments_it_gets() {
    let fixture = common::json("libm.json");
    let mut report = Vec::new();
    for (name, entry) in fixture.as_object().unwrap() {
        let cases = entry["cases"].as_array().unwrap();
        let mut differ = 0;
        for case in cases {
            let values: Vec<f64> = case.as_array().unwrap().iter().map(common::float).collect();
            let (args, expected) = values.split_at(values.len() - 1);
            let actual = match name.as_str() {
                "sin" => args[0].sin(),
                "cos" => args[0].cos(),
                "asin" => args[0].asin(),
                "sqrt" => args[0].sqrt(),
                "exp" => args[0].exp(),
                "atan2" => args[0].atan2(args[1]),
                "hypot" => py_hypot(args),
                other => panic!("unexpected function {other}"),
            };
            if actual.to_bits() != expected[0].to_bits() {
                differ += 1;
            }
        }
        if differ > 0 {
            report.push(format!("{name}: {differ} of {} differ", cases.len()));
        }
        assert!(!cases.is_empty() || name == "exp", "{name}: no samples");
    }
    assert!(report.is_empty(), "{report:?}");
}

// --- tests/unit/realtime/test_path.py ---------------------------------------------------------

const LON0: f64 = 14.0;
const LAT0: f64 = 50.0;
const LON_M: f64 = 1.399e-5;
const LAT_M: f64 = 8.993e-6;

fn at(x: f64, y: f64) -> LonLat {
    (LON0 + x * LON_M, LAT0 + y * LAT_M)
}

#[test]
fn a_zavlek_stop_nearer_the_return_lane_keeps_its_outbound_visit() {
    // 552650:1135 at Brňany,záv. (2026-10-12): a side road driven out and back, two lanes 5 m
    // apart, the stop pin a metre nearer the return lane. Each visit takes its own leg.
    let stop = |name: &str| match name {
        "A" => at(0.0, 0.0),
        "J" => at(1003.0, -200.0),
        "B" => at(1002.5, -1000.0),
        "E" => at(1002.5, -2000.0),
        "D" => at(3000.0, 0.0),
        _ => unreachable!(),
    };
    let calls = [
        ("A", "08:00"),
        ("J", "08:02"),
        ("B", "08:03"),
        ("E", "08:05"),
        ("B", "08:07"),
        ("J", "08:08"),
        ("D", "08:10"),
    ];
    let located: Vec<Option<LonLat>> = calls.iter().map(|(s, _)| Some(stop(s))).collect();
    let times: Vec<i64> = calls
        .iter()
        .map(|(_, t)| {
            let (h, m) = t.split_once(':').unwrap();
            h.parse::<i64>().unwrap() * 3600 + m.parse::<i64>().unwrap() * 60
        })
        .collect();
    let points: Vec<LonLat> = [
        (0.0, 0.0),
        (1000.0, 0.0),
        (1000.0, -2000.0),
        (1005.0, -2000.0),
        (1005.0, 0.0),
        (3000.0, 0.0),
    ]
    .iter()
    .map(|&(x, y)| at(x, y))
    .collect();
    let distances = cumulative_m(&points);
    let path = build_path::<()>(
        &located,
        Some((&points, &distances)),
        |i| Ok(times[i]),
        &policy().progress,
    )
    .unwrap();
    let expected = [0.0, 1200.0, 2000.0, 3002.5, 4005.0, 4805.0, 7000.0];
    let placed: Vec<f64> = path
        .call_distances_m
        .iter()
        .map(|d| d.round_ties_even())
        .collect();
    let wanted: Vec<f64> = expected.iter().map(|d: &f64| d.round_ties_even()).collect();
    assert_eq!(placed, wanted);
}
