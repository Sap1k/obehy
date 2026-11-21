//! `timeline/path.rs`: building a path from a shape and its calls (the Python worker's
//! `test_path.py`) and Douglas-Peucker.

use std::path::PathBuf;

use obehy_core::config::Policy;
use obehy_core::config::policy::POLICY_FILE;
use obehy_core::geo::{LonLat, cumulative_m};
use obehy_core::timeline::path::{build_path, douglas_peucker};

fn policy() -> Policy {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/obehy/data/realtime")
        .join(POLICY_FILE);
    Policy::load(&path).unwrap()
}

#[test]
fn douglas_peucker_keeps_the_ends_and_the_far_vertices() {
    let points = [
        (0.0, 0.0),
        (1.0, 0.1),
        (2.0, -0.1),
        (3.0, 5.0),
        (4.0, 6.0),
        (5.0, 7.0),
    ];
    assert_eq!(douglas_peucker(&points, 0.5), vec![0, 2, 3, 5]);
    assert_eq!(douglas_peucker(&points, 100.0), vec![0, 5]);
    assert_eq!(douglas_peucker(&points[..2], 0.5), vec![0, 1]);
    assert!(douglas_peucker(&[], 0.5).is_empty());
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
    assert_eq!(path.call_distances_m.len(), expected.len());
    for (placed, wanted) in path.call_distances_m.iter().zip(expected) {
        assert!((placed - wanted).abs() < 1.0, "{:?}", path.call_distances_m);
    }
}
