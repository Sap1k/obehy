//! The vehicle book and pairings: `tests/unit/realtime/test_pairing.py` and the book part of
//! `test_rail_fusion.py`, rewritten, plus the parts the Python tests reach only through the
//! database (loading stored pairings and attributes) and the proposed eviction rule.

mod common;

use std::collections::BTreeSet;

use common::builder::local;
use obehy_core::model::{Feed, JourneyKey, VehicleId};
use obehy_core::time::{Date, Instant, TimeDelta};
use obehy_core::vehicles::book::AttributeRow;
use obehy_core::vehicles::pairing::Settings;
use obehy_core::vehicles::registry::{Entry, RegistryKey};
use obehy_core::vehicles::vocabulary::VOCABULARY_FILE;
use obehy_core::vehicles::{
    BoundBy, Evidence, Fix, Pairings, Registry, Report, Sighting, VehicleBook, Vocabulary,
};

fn vocabulary() -> Vocabulary {
    let path = common::repo().join("src/obehy/data").join(VOCABULARY_FILE);
    Vocabulary::load(&path).expect("the vocabulary loads")
}

fn settings() -> Settings {
    vocabulary().pairing
}

fn journey(key: &str) -> JourneyKey {
    JourneyKey {
        feed: Feed::Jdf,
        namespace: "cis:line_trip".into(),
        key: key.into(),
        service_date: Date::ymd(2026, 10, 20),
    }
}

fn trip() -> JourneyKey {
    journey("582492:143")
}

fn other() -> JourneyKey {
    journey("582492:145")
}

fn r(source: &str, vehicle: &str) -> Report {
    (source.to_owned(), vehicle.to_owned())
}

fn duk() -> Report {
    r("duk", "807")
}

fn mpv() -> Report {
    r("mpvnet", "4U1 2345")
}

fn dpmd() -> Report {
    r("dpmd", "301")
}

const LON: f64 = 13.86;
const LAT: f64 = 50.66;
const STEP: f64 = 0.0005; // about 35 m of longitude

fn fix(report: Report, journey: JourneyKey, lon: f64, at: Instant) -> Fix {
    Fix {
        report,
        journey,
        lon,
        lat: LAT,
        at,
    }
}

fn tick(pairings: &mut Pairings, at: &str, gap: f64, journey: JourneyKey) -> BTreeSet<Report> {
    let t = local(at);
    let fixes = [
        fix(duk(), trip(), LON, t),
        fix(mpv(), journey, LON + gap, t - TimeDelta::from_seconds(10)),
    ];
    pairings.observe(&fixes, t)
}

fn set(reports: &[Report]) -> BTreeSet<Report> {
    reports.iter().cloned().collect()
}

#[test]
fn a_duk_id_and_an_mpvnet_plate_on_one_journey_become_one_vehicle() {
    let mut pairings = Pairings::new(settings());
    assert!(tick(&mut pairings, "2026-10-20 08:00", STEP, trip()).is_empty());
    assert!(tick(&mut pairings, "2026-10-20 08:01", STEP, trip()).is_empty());
    assert!(pairings.partners(&duk()).is_empty()); // not yet sustained
    let moved = tick(&mut pairings, "2026-10-20 08:02", STEP, trip());
    assert_eq!(moved, set(&[duk(), mpv()])); // confirmed
    assert_eq!(pairings.partners(&duk()), vec![mpv()]);
    assert_eq!(pairings.partners(&mpv()), vec![duk()]);
    let changes = pairings.changes();
    assert_eq!(changes.len(), 1);
    let (pair, evidence) = changes.into_iter().next().unwrap();
    assert_eq!(pair, (duk(), mpv()));
    assert!(evidence.is_some_and(|e| e.confirmed));
}

#[test]
fn they_part_when_apart_on_another_journey_or_long_unseen() {
    for parting in ["apart", "another journey", "unseen"] {
        let mut pairings = Pairings::new(settings());
        for minute in 0..3 {
            tick(
                &mut pairings,
                &format!("2026-10-20 08:0{minute}"),
                STEP,
                trip(),
            );
        }
        assert_eq!(pairings.partners(&duk()), vec![mpv()]);
        let moved = match parting {
            "apart" => tick(&mut pairings, "2026-10-20 08:04", 0.03, trip()), // about 2 km
            "another journey" => tick(&mut pairings, "2026-10-20 08:04", STEP, other()),
            _ => pairings.observe(&[], local("2026-10-20 10:00")),
        };
        assert_eq!(moved, set(&[duk(), mpv()]), "{parting}");
        assert!(pairings.partners(&duk()).is_empty(), "{parting}");
    }
}

#[test]
fn one_source_is_never_paired_with_itself() {
    let mut pairings = Pairings::new(settings());
    let other_duk = r("duk", "808");
    for minute in 0..4 {
        let t = local(&format!("2026-10-20 08:0{minute}"));
        let fixes = [
            fix(duk(), trip(), LON, t),
            fix(other_duk.clone(), trip(), LON, t),
        ];
        pairings.observe(&fixes, t);
    }
    assert!(pairings.partners(&duk()).is_empty());
}

fn three(pairings: &mut Pairings, minute: u32, with_dpmd: bool) -> BTreeSet<Report> {
    let t = local(&format!("2026-10-20 08:0{minute}"));
    let mut fixes = vec![
        fix(duk(), trip(), LON, t),
        fix(mpv(), trip(), LON + STEP, t),
    ];
    if with_dpmd {
        fixes.push(fix(dpmd(), trip(), LON + 2.0 * STEP, t));
    }
    pairings.observe(&fixes, t)
}

#[test]
fn pairs_join_transitively_into_one_vehicle() {
    let mut pairings = Pairings::new(settings());
    for minute in 0..3 {
        three(&mut pairings, minute, true);
    }
    let mut expected = vec![duk(), mpv(), dpmd()];
    expected.sort();
    assert_eq!(pairings.cluster(&mpv()), expected);
}

#[test]
fn a_co_located_vehicle_keeps_its_id_as_reports_join_and_leave() {
    let mut book = VehicleBook::new(Registry::default(), vocabulary());
    let day = Date::ymd(2026, 10, 20);
    // MPVnet and DPMD first: DPMD names the vehicle (both unranked).
    for minute in 0..3 {
        let t = local(&format!("2026-10-20 08:0{minute}"));
        let fixes = [
            fix(mpv(), trip(), LON, t),
            fix(dpmd(), trip(), LON + STEP, t),
        ];
        for report in book.pairings.observe(&fixes, t) {
            book.identify(&report, day);
        }
    }
    let co_located = ("dpmd:301".to_owned(), Some(BoundBy::CoLocated));
    assert_eq!(book.identify(&mpv(), day), co_located);
    // DÚK, ranked first, joins: the vehicle keeps its ID.
    for minute in 3..6 {
        let moved = three(&mut book.pairings, minute, true);
        for report in moved {
            for member in book.pairings.cluster(&report) {
                book.identify(&member, day);
            }
        }
    }
    for report in [duk(), mpv(), dpmd()] {
        assert_eq!(book.identify(&report, day), co_located);
    }
    // GTFS-RT names the vehicle as the state tables do.
    let named = |s: &str, v: &str| VehicleId {
        source: s.into(),
        source_vehicle_id: v.into(),
    };
    assert_eq!(book.vehicle_id(&named("duk", "807")), "dpmd:301");
    assert_eq!(book.vehicle_id(&named("duk", "999")), "duk:999");
    // History keeps the vehicle a report was: the co-located one, as the map names it.
    assert_eq!(book.worked_by(&mpv(), day), "dpmd:301");
}

fn on_run(report: Report, train: &str, at: Instant) -> Sighting {
    Sighting {
        report,
        last_seen: at,
        train: Some(train.to_owned()),
        fix: None,
    }
}

#[test]
fn history_keeps_a_rail_runs_unit_not_its_train() {
    let mut book = VehicleBook::new(Registry::default(), vocabulary());
    let day = Date::ymd(2026, 10, 20);
    book.track_sightings(vec![on_run(duk(), "train:6402", local("2026-10-20 08:00"))]);
    // The map: the train; history: the unit (the key is the train).
    assert_eq!(
        book.identify(&duk(), day),
        ("train:6402".to_owned(), Some(BoundBy::Train))
    );
    assert_eq!(book.worked_by(&duk(), day), "duk:807");
}

#[test]
fn every_report_on_a_run_is_its_train_and_one_leaving_is_itself_again() {
    let at = local("2026-10-08 08:01");
    let reports = [
        r("duk", "950"),
        r("duk", "951"),
        r("pid", "train:100"),
        r("sz-mapa", "TR/1"),
    ];
    let mut book = VehicleBook::new(Registry::default(), vocabulary());
    book.track_sightings(
        reports
            .iter()
            .map(|r| on_run(r.clone(), "train:100", at))
            .collect(),
    );
    let day = Date::ymd(2026, 10, 8);
    for report in &reports {
        assert_eq!(
            book.identify(report, day),
            ("train:100".to_owned(), Some(BoundBy::Train))
        );
    }
    let mut left = reports
        .iter()
        .map(|r| on_run(r.clone(), "train:100", at))
        .collect::<Vec<_>>();
    left.retain(|s| s.report != r("duk", "951"));
    left.push(Sighting {
        report: r("duk", "951"),
        last_seen: at,
        train: None,
        fix: None,
    });
    book.track_sightings(left);
    assert_eq!(
        book.identify(&r("duk", "951"), day),
        ("duk:951".to_owned(), None)
    );
    assert!(book.dirty().contains("train:100") && book.dirty().contains("duk:951"));
}

#[test]
fn a_registry_key_names_a_report_by_source_or_by_a_stated_identifier() {
    let since = Date::ymd(2026, 1, 1);
    let key = |id: &str, priority: i64| RegistryKey {
        vehicle_id: id.into(),
        priority,
        valid_from: since,
        valid_to: None,
    };
    let mut registry = Registry::default();
    registry.by_source.insert(duk(), vec![key("reg:1", 2)]);
    registry.by_identifier.insert(
        ("licence_plate".into(), "4U12345".into()),
        vec![key("reg:2b", 2), key("reg:2", 1)],
    );
    registry.entries.insert(
        "reg:1".into(),
        Entry {
            vehicle_id: "reg:1".into(),
            public_label: Some("807".into()),
            manufacturer: None,
            model: None,
            datasets: vec!["own".into()],
        },
    );
    let at = local("2026-10-20 08:00");
    let mut book = VehicleBook::load(
        registry,
        vocabulary(),
        Vec::new(),
        vec![AttributeRow {
            source: mpv().0,
            source_vehicle_id: mpv().1,
            attribute: "licence_plate".into(),
            value: "4u1-2345".into(),
            at,
        }],
    );
    let day = Date::ymd(2026, 10, 20);
    assert_eq!(
        book.identify(&duk(), day),
        ("reg:1".to_owned(), Some(BoundBy::Source))
    );
    // The better dataset (lower priority) wins.
    assert_eq!(
        book.identify(&mpv(), day),
        ("reg:2".to_owned(), Some(BoundBy::Identifier))
    );
    // Loading rewrites every profile: the registered vehicles and the reports' own.
    let rows = book.rows(Vec::new());
    assert!(rows.rewrite);
    let names: Vec<&str> = rows
        .profiles
        .iter()
        .map(|p| p.vehicle_id.as_str())
        .collect();
    assert_eq!(names, vec!["reg:1", "reg:2"]);
    assert_eq!(rows.profiles[1].text("licence_plate"), Some("4u1-2345"));
    assert!(!book.rows(Vec::new()).rewrite);
}

#[test]
fn stored_pairings_load_and_the_proposed_eviction_forgets_unseen_reports() {
    let at = local("2026-10-20 08:00");
    let evidence = Evidence {
        first_seen: at,
        last_seen: at,
        fixes: 3,
        journey: trip(),
        confirmed: true,
    };
    let row = |report: &Report, at: Instant| AttributeRow {
        source: report.0.clone(),
        source_vehicle_id: report.1.clone(),
        attribute: "operator".into(),
        value: "x".into(),
        at,
    };
    let mut book = VehicleBook::load(
        Registry::default(),
        vocabulary(),
        vec![((duk(), mpv()), evidence)],
        vec![row(&duk(), at), row(&mpv(), at + TimeDelta::from_days(3))],
    );
    // Loaded as confirmed: one vehicle, named by the ranked source.
    let day = Date::ymd(2026, 10, 23);
    assert_eq!(book.identify(&mpv(), day).0, "duk:807");
    assert_eq!(book.sizes(), (2, 2, 1));
    let forgotten = book.forget_unseen(at + TimeDelta::from_days(1));
    assert_eq!(forgotten, 1);
    assert_eq!(book.sizes(), (1, 1, 1));
    assert_eq!(
        book.identities().keys().cloned().collect::<Vec<_>>(),
        vec![mpv()]
    );
}
