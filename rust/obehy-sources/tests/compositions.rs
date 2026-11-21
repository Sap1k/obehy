//! ČD compositions, the plan side (test_compositions.py): the request from what the worker
//! knows of a train (CD-Q1), which call it is asked from, and the manifest's `applies`. The
//! decode side is in python_cases.rs (every sample answer, compared whole).

use obehy_core::compositions::{NotAvailable, Subject, TripView, ViewCall};
use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::time::{Date, Instant};
use obehy_sources::cd;
use std::collections::BTreeMap;

const DAY: Date = Date::ymd(2026, 10, 20);

/// Train 607 from Cheb: Cheb, Sokolov, a passing point, Karlovy Vary, Praha hl.n.; the first
/// `left` calls left.
fn view(left: i64, company: &str, day: Date, sr70: bool) -> TripView {
    let stations = [
        ("75035", true),
        ("74013", true),
        ("74015", false),
        ("72015", true),
    ];
    let mut calls: Vec<ViewCall> = stations
        .iter()
        .enumerate()
        .map(|(n, (code, passenger))| ViewCall {
            ordinal: n as i64,
            location_id: format!("czptt:stop:CZ:{code}"),
            visit_n: 1,
            passenger: *passenger,
            left: (n as i64) < left,
            keys: if sr70 {
                BTreeMap::from([("sr70".to_owned(), (*code).to_owned())])
            } else {
                BTreeMap::new()
            },
        })
        .collect();
    calls.push(ViewCall {
        ordinal: 4,
        location_id: "czptt:stop:CZ:54700".into(),
        visit_n: 1,
        passenger: true,
        left: left > 4,
        keys: BTreeMap::new(),
    });
    TripView {
        feed: "czptt".into(),
        trip_id: "czptt:trip:607".into(),
        service_date: day,
        now: Instant::from_isoformat("2026-10-20T09:00:00+00:00").unwrap(),
        keys: BTreeMap::from([
            ("czptt:train_number".to_owned(), "607".to_owned()),
            (
                "czptt:tr".to_owned(),
                format!("Tr:{company}:--KADR000607:00:2026"),
            ),
        ]),
        calls,
    }
}

fn plain(left: i64) -> TripView {
    view(left, "1154", DAY, true)
}

fn subject(location: Option<&str>) -> Subject {
    Subject {
        feed: "czptt".into(),
        trip_id: "czptt:trip:607".into(),
        service_date: DAY,
        from_location: location.map(str::to_owned),
    }
}

#[test]
fn cd_q1_the_station_is_the_country_code_and_the_sr70() {
    let planned = cd::plan(&subject(None), &plain(0)).unwrap();
    assert_eq!(planned.cache_key, "607/2026-10-20/5475035");
    assert_eq!(planned.start.location_id, "czptt:stop:CZ:75035");
    assert_eq!(
        planned.params,
        vec![(
            "params".to_owned(),
            r#"[{"TrainNum":"607","SR70":"5475035","Date":"20.10.2026","GetPlan":"false"}]"#
                .to_owned()
        )]
    );
}

#[test]
fn the_plan_asks_from_the_next_passenger_call_never_the_last() {
    assert_eq!(
        cd::plan(&subject(None), &plain(2)).unwrap().start.ordinal,
        3
    );
    assert_eq!(
        cd::plan(&subject(None), &plain(4)).unwrap().start.ordinal,
        3
    );
    let terminus = subject(Some("czptt:stop:CZ:54700"));
    assert_eq!(cd::plan(&terminus, &plain(0)).unwrap().start.ordinal, 3);
    let left = subject(Some("czptt:stop:CZ:74013"));
    assert_eq!(cd::plan(&left, &plain(3)).unwrap().start.ordinal, 1);
    assert!(cd::plan(&subject(Some("czptt:stop:CZ:74015")), &plain(0)).is_err());
}

#[test]
fn the_plan_knows_what_cd_cannot_answer() {
    assert_eq!(
        cd::plan(&subject(None), &view(0, "1154", DAY, false)),
        Err(NotAvailable::new("czptt:stop:CZ:75035 has no SR70 code"))
    );
    let mut numberless = plain(0);
    numberless.keys.remove("czptt:train_number");
    assert_eq!(
        cd::plan(&subject(None), &numberless),
        Err(NotAvailable::new("the train has no train number"))
    );
}

#[test]
fn the_cd_manifest_says_which_trains_it_answers_for() {
    let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/obehy/data/realtime/sources");
    let sources = manifest::load_sources(&dir, &RECORDER_FILTERS).unwrap();
    let lookup = sources
        .iter()
        .flat_map(|s| &s.lookups)
        .find(|l| l.source == "cd")
        .unwrap();
    for (v, reason) in [
        (plain(0), None),
        (
            view(0, "3246", DAY, true),
            Some("not a trip it answers for (czptt:tr)"),
        ),
        (view(0, "1154", Date::ymd(2026, 10, 21), true), None),
        (
            view(0, "1154", Date::ymd(2026, 10, 25), true),
            Some("answers only within 1 day(s) of today"),
        ),
    ] {
        let keys = |ns: &str| v.keys.get(ns).cloned();
        assert_eq!(
            lookup
                .applies
                .refusal(&keys, v.service_date, DAY)
                .as_deref(),
            reason
        );
    }
}
