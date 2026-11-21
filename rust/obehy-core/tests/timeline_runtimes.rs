//! `timeline/runtimes.rs`: the unit tests of the Python worker's `test_runtimes.py`.

mod common;

use std::path::PathBuf;

use obehy_core::config::Policy;
use obehy_core::config::policy::POLICY_FILE;
use obehy_core::model::{HopKey, Traversal};
use obehy_core::time::{Instant, TimeDelta};
use obehy_core::timeline::runtimes::{Hops, forget, ratio};

fn policy() -> Policy {
    let path: PathBuf = common::repo()
        .join("src/obehy/data/realtime")
        .join(POLICY_FILE);
    Policy::load(&path).unwrap()
}

// --- tests/unit/realtime/test_runtimes.py -----------------------------------------------------

/// 2026-10-08 08:00 Prague (06:00 UTC).
const NOW: Instant = Instant::from_unix_seconds(1_791_439_200);

fn hop() -> HopKey {
    HopKey {
        mode: "bus".into(),
        from: "jdf:A".into(),
        to: "jdf:B".into(),
        from_departure: false,
    }
}

/// Traversals ending `minutes` after 08:00 (negative: before).
fn hops(traversals: &[(i64, f64, f64)]) -> Hops {
    let mut hops = Hops::default();
    hops.insert(
        hop(),
        traversals
            .iter()
            .map(|&(minutes, taken, planned)| Traversal {
                end: NOW + TimeDelta::from_minutes(minutes),
                taken_s: taken,
                scheduled_s: planned,
            })
            .collect(),
    );
    hops
}

#[test]
fn a_hop_no_one_drove_has_no_ratio() {
    let policy = policy();
    assert_eq!(
        ratio(&Hops::default(), &hop(), NOW, &policy.prediction),
        None
    );
    assert_eq!(ratio(&hops(&[]), &hop(), NOW, &policy.prediction), None);
}

#[test]
fn one_traversal_moves_the_ratio_only_part_of_the_way() {
    let p = policy().prediction;
    let value = ratio(&hops(&[(0, 120.0, 60.0)]), &hop(), NOW, &p);
    let prior = p.runtime_prior_weight;
    assert_eq!(value, Some((prior + 2.0) / (prior + 1.0)));
}

#[test]
fn older_traversals_weigh_less() {
    let p = policy().prediction;
    let value = ratio(
        &hops(&[(-30, 120.0, 60.0), (0, 60.0, 60.0)]),
        &hop(),
        NOW,
        &p,
    )
    .unwrap();
    let old = (-1800.0 / p.runtime_decay_s as f64).exp();
    let prior = p.runtime_prior_weight;
    assert!((value - (prior + old * 2.0 + 1.0) / (prior + old + 1.0)).abs() < 1e-9);
}

#[test]
fn a_wild_traversal_is_clipped_and_a_future_one_ignored() {
    let p = policy().prediction;
    let value = ratio(
        &hops(&[(0, 6000.0, 60.0), (5, 60.0, 60.0)]),
        &hop(),
        NOW,
        &p,
    );
    let (prior, most) = (p.runtime_prior_weight, p.runtime_ratio_max);
    assert_eq!(value, Some((prior + most) / (prior + 1.0)));
}

#[test]
fn hops_not_driven_for_a_while_are_forgotten() {
    let p = policy().prediction;
    let mut hops = hops(&[(-120, 60.0, 60.0)]);
    forget(&mut hops, NOW, &p);
    assert!(hops.is_empty());
    assert!(p.runtime_keep_decays * p.runtime_decay_s < 7200);
}

#[test]
fn forget_reads_the_last_appended_not_the_latest() {
    // Ends are not sorted: the last appended decides, as `v[-1]` does in Python.
    let p = policy().prediction;
    let mut hops = hops(&[(0, 60.0, 60.0), (-120, 60.0, 60.0)]);
    forget(&mut hops, NOW, &p);
    assert!(hops.is_empty());
}
