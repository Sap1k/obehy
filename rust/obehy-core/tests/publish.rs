//! `publish` and `vehicles` against Python (R7): `rust/tools/gen_publish_fixtures.py` ran real
//! windows through the Python runner and recorded every call the writer made to its vehicle
//! book, and at some snapshots the whole feed states with what Python published from them
//! (GTFS-RT bytes, `representing`, `publication`); plus every GTFS-RT serialization of the unit
//! tests and alert entities of hand-made rows. Here the book replays the calls in order and the
//! states are rebuilt from their JSON: every output must be equal, the GTFS-RT byte for byte.

mod common;

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;

use obehy_core::config::Policy;
use obehy_core::config::policy::POLICY_FILE;
use obehy_core::model::state_json::{self, feed_state, float, instant, int, journey, text};
use obehy_core::model::{
    BindingMethod, Fact, Feed, FeedState, JourneyKey, Observation, ObservationResult, RawRef,
    VehicleAttribute, VehicleId, VehicleKey,
};
use obehy_core::publish::alerts::{
    AlertEntities, AlertPeriod, AlertRow, AlertTarget, alert_entity, over,
};
use obehy_core::publish::gtfs_rt::FeedEmitter;
use obehy_core::publish::represent::{publication, representing};
use obehy_core::time::{Date, Instant};
use obehy_core::vehicles::precedence::{Profile, Statement};
use obehy_core::vehicles::registry::{CuratedValue, Entry, RegistryKey};
use obehy_core::vehicles::vocabulary::VOCABULARY_FILE;
use obehy_core::vehicles::{
    AttributeRow, Evidence, Fix, Pair, Registry, Report, Sighting, VehicleBook, VehicleRows,
    Vocabulary,
};
use serde_json::{Value, json};

fn policy() -> Policy {
    let path = common::repo()
        .join("src/obehy/data/realtime")
        .join(POLICY_FILE);
    Policy::load(&path).expect("the policy loads")
}

fn vocabulary() -> Vocabulary {
    let path = common::repo().join("src/obehy/data").join(VOCABULARY_FILE);
    Vocabulary::load(&path).expect("the vocabulary loads")
}

fn base64(text: &str) -> Vec<u8> {
    let value = |c: u8| -> u32 {
        match c {
            b'A'..=b'Z' => u32::from(c - b'A'),
            b'a'..=b'z' => u32::from(c - b'a') + 26,
            b'0'..=b'9' => u32::from(c - b'0') + 52,
            b'+' => 62,
            b'/' => 63,
            _ => panic!("not base64: {c}"),
        }
    };
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    for chunk in text.as_bytes().chunks(4) {
        let data: Vec<u8> = chunk.iter().copied().filter(|c| *c != b'=').collect();
        let mut acc = 0u32;
        for (i, c) in data.iter().enumerate() {
            acc |= value(*c) << (18 - 6 * i);
        }
        for i in 0..data.len().saturating_sub(1) {
            out.push((acc >> (16 - 8 * i)) as u8);
        }
    }
    out
}

fn s(value: &Value) -> &str {
    value.as_str().expect("a string")
}

fn report(value: &Value) -> Report {
    (s(&value[0]).to_owned(), s(&value[1]).to_owned())
}

fn day(value: &Value) -> Date {
    Date::from_isoformat(s(value)).expect("a date")
}

fn journey_list(j: &JourneyKey) -> Value {
    json!([
        j.feed.as_str(),
        j.namespace,
        j.key,
        j.service_date.isoformat()
    ])
}

fn journey_of_list(value: &Value) -> JourneyKey {
    JourneyKey {
        feed: s(&value[0]).parse().expect("a feed"),
        namespace: s(&value[1]).to_owned(),
        key: s(&value[2]).to_owned(),
        service_date: day(&value[3]),
    }
}

fn identity_map(value: &Value) -> BTreeMap<String, String> {
    value
        .as_array()
        .expect("pairs")
        .iter()
        .map(|kv| (s(&kv[0]).to_owned(), s(&kv[1]).to_owned()))
        .collect()
}

// --- the vehicle book's outputs as the generator encodes Python's -----------------------------

fn attribute_rows_json(rows: &[AttributeRow]) -> Value {
    rows.iter()
        .map(|r| {
            json!([
                r.source,
                r.source_vehicle_id,
                r.attribute,
                r.value,
                r.at.micros()
            ])
        })
        .collect()
}

fn attribute_rows_of(value: &Value) -> Vec<AttributeRow> {
    value
        .as_array()
        .expect("rows")
        .iter()
        .map(|r| AttributeRow {
            source: s(&r[0]).to_owned(),
            source_vehicle_id: s(&r[1]).to_owned(),
            attribute: s(&r[2]).to_owned(),
            value: s(&r[3]).to_owned(),
            at: Instant::from_micros(int(&r[4]).expect("an instant")),
        })
        .collect()
}

fn pair_json(pair: &Pair) -> Value {
    json!([[pair.0.0, pair.0.1], [pair.1.0, pair.1.1]])
}

fn evidence_json(e: &Evidence) -> Value {
    json!([
        e.first_seen.micros(),
        e.last_seen.micros(),
        e.fixes,
        journey_list(&e.journey),
        e.confirmed
    ])
}

fn statement_json(st: &Statement) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::new();
    out.insert("value".into(), json!(st.value));
    out.insert("origin".into(), json!(st.origin.as_str()));
    out.insert("source".into(), json!(st.source));
    out.insert(
        "observed_at".into(),
        st.observed_at
            .map_or(Value::Null, |at| json!(at.isoformat())),
    );
    out
}

fn profile_json(p: &Profile) -> Value {
    let flags: serde_json::Map<String, Value> = p
        .flags
        .iter()
        .map(|(k, v)| ((*k).to_owned(), json!(v)))
        .collect();
    let texts: serde_json::Map<String, Value> = p
        .texts
        .iter()
        .map(|(k, v)| ((*k).to_owned(), json!(v)))
        .collect();
    let attributes: Vec<Value> = p
        .attributes
        .iter()
        .map(|(attribute, picked)| {
            let mut out = statement_json(&picked.winner);
            out.insert("attribute".into(), json!(attribute));
            out.insert("conflict".into(), json!(picked.conflict));
            out.insert(
                "statements".into(),
                picked
                    .statements
                    .iter()
                    .map(|st| Value::Object(statement_json(st)))
                    .collect(),
            );
            Value::Object(out)
        })
        .collect();
    json!({
        "vehicle_id": p.vehicle_id,
        "registered": p.registered,
        "public_label": p.public_label,
        "flags": flags,
        "texts": texts,
        "datasets": p.datasets,
        "sources": p.sources,
        "attributes": attributes,
        "last_stated": p.last_stated.map(|at| at.micros()),
    })
}

fn vehicle_rows_json(rows: &VehicleRows) -> Value {
    json!({
        "gone": rows.gone.iter().map(pair_json).collect::<Vec<_>>(),
        "kept": rows.kept.iter().map(|(p, e)| json!([pair_json(p), evidence_json(e)])).collect::<Vec<_>>(),
        "rewrite": rows.rewrite,
        "profiles": rows.profiles.iter().map(profile_json).collect::<Vec<_>>(),
    })
}

fn sightings_of(value: &Value) -> Vec<Sighting> {
    value
        .as_array()
        .expect("sightings")
        .iter()
        .map(|v| {
            let report = (s(&v[0]).to_owned(), s(&v[1]).to_owned());
            let fix = (!v[4].is_null()).then(|| Fix {
                report: report.clone(),
                journey: journey_of_list(&v[4][0]),
                lon: float(&v[4][1]).expect("lon"),
                lat: float(&v[4][2]).expect("lat"),
                at: instant(&v[4][3]).expect("at"),
            });
            Sighting {
                report,
                last_seen: instant(&v[2]).expect("last seen"),
                train: v[3].as_str().map(str::to_owned),
                fix,
            }
        })
        .collect()
}

fn results_of(value: &Value) -> Vec<ObservationResult> {
    value
        .as_array()
        .expect("results")
        .iter()
        .map(|r| {
            let mut facts: Vec<Fact> = vec![
                VehicleKey {
                    source_vehicle_id: s(&r[1]).to_owned(),
                }
                .into(),
            ];
            for a in r[3].as_array().expect("attributes") {
                facts.push(
                    VehicleAttribute {
                        attribute: s(&a[0]).to_owned(),
                        value: s(&a[1]).to_owned(),
                    }
                    .into(),
                );
            }
            ObservationResult {
                observation: Arc::new(Observation {
                    source: s(&r[0]).to_owned(),
                    channel: String::new(),
                    feed: Feed::Jdf,
                    received_at: instant(&r[2]).expect("received"),
                    observed_at: None,
                    raw: RawRef {
                        sha256: String::new(),
                        item: 0,
                    },
                    decoder_version: 0,
                    facts,
                }),
                journey: None,
                reason: None,
                public: Vec::new(),
                state_since: None,
                method: BindingMethod::Keyed,
            }
        })
        .collect()
}

fn checkpoint_json(book: &VehicleBook) -> Value {
    json!({
        "identity": book.identities().iter().map(|(r, (id, by))| {
            json!([[r.0, r.1], id, by.map(|b| b.as_str())])
        }).collect::<Vec<_>>(),
        "pairs": book.pairings.pairs.iter().map(|(p, e)| json!([pair_json(p), evidence_json(e)])).collect::<Vec<_>>(),
        "trains": book.trains().iter().map(|(r, t)| json!([[r.0, r.1], t])).collect::<Vec<_>>(),
        "clock": book.clock().map(|c| c.micros()),
    })
}

// --- the windows ------------------------------------------------------------------------------

fn check_snapshot(
    snapshot: &Value,
    book: &VehicleBook,
    sightings: &[Sighting],
    emitters: &mut BTreeMap<String, FeedEmitter>,
    policy: &Policy,
) -> usize {
    let at = instant(&snapshot["at"]).expect("at");
    let mut states: Vec<FeedState> = Vec::new();
    let mut entities = 0;
    for feed in snapshot["feeds"].as_array().expect("feeds") {
        let state = feed_state(&feed["state"]).expect("the state reads");
        let name = s(&feed["feed"]);
        let by_name: BTreeMap<String, &VehicleId> =
            state.vehicles.keys().map(|v| (v.to_string(), v)).collect();
        for (vehicle, expected) in identity_map(&feed["identity"]) {
            let v = by_name[&vehicle];
            assert_eq!(book.vehicle_id(v), expected, "{name} identity of {vehicle}");
        }
        let chosen: Vec<String> = representing(&state).iter().map(|v| v.to_string()).collect();
        let expected: Vec<String> = feed["representing"]
            .as_array()
            .expect("representing")
            .iter()
            .map(|v| s(v).to_owned())
            .collect();
        assert_eq!(chosen, expected, "{name} representing");
        for (instance, expected) in state
            .instances
            .values()
            .zip(feed["publication"].as_array().expect("publication"))
        {
            assert_eq!(journey_list(&instance.journey), expected[0]);
            let got = publication(instance, at, &policy.lifecycle).map(|p| p.as_str());
            assert_eq!(got, expected[1].as_str(), "{:?}", instance.journey);
        }
        let expected = base64(s(&feed["gtfs_rt"]));
        let emitter = emitters
            .entry(name.to_owned())
            .or_insert_with(|| FeedEmitter::new(policy.lifecycle.clone()));
        let identity = |v: &VehicleId| book.vehicle_id(v);
        let payload = emitter.serialize::<&[u8]>(&state, at, &[], &identity);
        assert!(
            payload == expected,
            "{name} GTFS-RT at {at}: {} vs {} bytes",
            payload.len(),
            expected.len()
        );
        // Again, from the cache.
        assert!(emitter.serialize::<&[u8]>(&state, at, &[], &identity) == expected);
        entities += emitter.cached();
        states.push(state);
    }
    let read = VehicleBook::sightings(states.iter());
    assert!(read == sightings, "the sightings read from the states");
    entities
}

/// Replays a recorded log of book calls, asserting every output; `checkpoint` sees the book
/// and the last sightings at each checkpoint (by entry index). The number of calls per op.
fn replay_book(
    name: &str,
    book: &mut VehicleBook,
    entries: &Value,
    mut checkpoint: impl FnMut(usize, &VehicleBook, &[Sighting]),
) -> BTreeMap<String, usize> {
    let mut observed: Vec<AttributeRow> = Vec::new();
    let mut sightings: Vec<Sighting> = Vec::new();
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for (i, entry) in entries.as_array().expect("book").iter().enumerate() {
        let op = s(&entry["op"]);
        *counts.entry(op.to_owned()).or_default() += 1;
        let at = || format!("{name} book call {i} ({op})");
        match op {
            "observe" => {
                observed = book.observe(&results_of(&entry["in"]));
                assert_eq!(attribute_rows_json(&observed), entry["out"], "{}", at());
            }
            "worked_by" => {
                let got = book.worked_by(&report(&entry["report"]), day(&entry["day"]));
                assert_eq!(got, s(&entry["out"]), "{}", at());
            }
            "identify" => {
                let (id, by) = book.identify(&report(&entry["report"]), day(&entry["day"]));
                assert_eq!(
                    json!([id, by.map(|b| b.as_str())]),
                    entry["out"],
                    "{}",
                    at()
                );
            }
            "rows" => {
                let given = if entry["in"] == "observed" {
                    observed.clone()
                } else {
                    attribute_rows_of(&entry["in"])
                };
                let rows = book.rows(given.clone());
                assert_eq!(rows.attributes, given);
                *counts.entry("profiles".to_owned()).or_default() += rows.profiles.len();
                assert_eq!(vehicle_rows_json(&rows), entry["out"], "{}", at());
            }
            "track" => {
                sightings = sightings_of(&entry["in"]);
                book.track_sightings(sightings.clone());
            }
            "reload" => book.reload(registry_of(&entry["registry"])),
            "checkpoint" => {
                let mut expected = entry.clone();
                expected.as_object_mut().expect("an object").remove("op");
                assert_eq!(checkpoint_json(book), expected, "{}", at());
                checkpoint(i, book, &sightings);
            }
            other => panic!("unknown op {other}"),
        }
    }
    counts
}

fn replay_window(name: &str) {
    let data = common::trace::read_gz_json(&common::path(&format!("publish/{name}.json.gz")));
    let policy = policy();
    let mut book = VehicleBook::new(Registry::default(), vocabulary());
    let snapshots: BTreeMap<usize, &Value> = data["snapshots"]
        .as_array()
        .expect("snapshots")
        .iter()
        .map(|s| (int(&s["checkpoint"]).expect("index") as usize, s))
        .collect();
    let mut emitters = BTreeMap::new();
    let mut checked = 0;
    let counts = replay_book(name, &mut book, &data["book"], |i, book, sightings| {
        if let Some(snapshot) = snapshots.get(&i) {
            check_snapshot(snapshot, book, sightings, &mut emitters, &policy);
            checked += 1;
        }
    });
    assert_eq!(checked, snapshots.len());
    assert!(counts["profiles"] > 0 && counts["track"] > 0, "{counts:?}");
    eprintln!("{name}: {counts:?}, {checked} snapshots");
}

fn registry_of(value: &Value) -> Registry {
    let keys = |v: &Value| -> HashMap<(String, String), Vec<RegistryKey>> {
        v.as_array()
            .expect("keys")
            .iter()
            .map(|kv| {
                let list = kv[1]
                    .as_array()
                    .expect("keys")
                    .iter()
                    .map(|k| RegistryKey {
                        vehicle_id: s(&k[0]).to_owned(),
                        priority: int(&k[1]).expect("priority"),
                        valid_from: day(&k[2]),
                        valid_to: (!k[3].is_null()).then(|| day(&k[3])),
                    })
                    .collect();
                (report(&kv[0]), list)
            })
            .collect()
    };
    let opt = |v: &Value| v.as_str().map(str::to_owned);
    Registry {
        by_source: keys(&value["by_source"]),
        by_identifier: keys(&value["by_identifier"]),
        entries: value["entries"]
            .as_array()
            .expect("entries")
            .iter()
            .map(|e| {
                let entry = Entry {
                    vehicle_id: s(&e[0]).to_owned(),
                    public_label: opt(&e[1]),
                    manufacturer: opt(&e[2]),
                    model: opt(&e[3]),
                    datasets: e[4]
                        .as_array()
                        .expect("datasets")
                        .iter()
                        .map(|d| s(d).to_owned())
                        .collect(),
                };
                (entry.vehicle_id.clone(), entry)
            })
            .collect(),
        values: value["values"]
            .as_array()
            .expect("values")
            .iter()
            .map(|kv| {
                let list = kv[1]
                    .as_array()
                    .expect("values")
                    .iter()
                    .map(|c| CuratedValue {
                        attribute: s(&c[0]).to_owned(),
                        value: s(&c[1]).to_owned(),
                        dataset: s(&c[2]).to_owned(),
                        priority: int(&c[3]).expect("priority"),
                        origin: s(&c[4]).to_owned(),
                    })
                    .collect();
                (s(&kv[0]).to_owned(), list)
            })
            .collect(),
    }
}

#[test]
fn a_scripted_book_binds_pairs_and_profiles_as_python() {
    let data = common::json("publish/book.json");
    let mut book = VehicleBook::new(registry_of(&data["registry"]), vocabulary());
    let mut checkpoints = 0;
    let counts = replay_book("book", &mut book, &data["book"], |_, _, _| checkpoints += 1);
    assert!(checkpoints > 5 && counts["reload"] == 1, "{counts:?}");
}

#[test]
fn the_pid_window_publishes_and_names_vehicles_as_python() {
    replay_window("pid");
}

#[test]
fn the_duk_sz_arriva_window_publishes_and_names_vehicles_as_python() {
    replay_window("duk-sz-arriva");
}

// --- the unit tests' scenarios ----------------------------------------------------------------

#[test]
fn the_unit_tests_gtfs_rt_is_byte_identical() {
    let data = common::trace::read_gz_json(&common::path("publish/scenarios.json.gz"));
    let policy = policy();
    let mut tests = BTreeSet::new();
    for call in data["calls"].as_array().expect("calls") {
        let test = s(&call["test"]);
        tests.insert(test);
        let state = feed_state(&call["state"]).expect("the state reads");
        match s(&call["op"]) {
            "serialize" => {
                let names = identity_map(&call["identity"]);
                let identity = |v: &VehicleId| names[&v.to_string()].clone();
                let alerts: Vec<Vec<u8>> = call["alerts"]
                    .as_array()
                    .expect("alerts")
                    .iter()
                    .map(|a| base64(s(a)))
                    .collect();
                let now = instant(&call["now"]).expect("now");
                let mut emitter = FeedEmitter::new(policy.lifecycle.clone());
                let payload = emitter.serialize(&state, now, &alerts, &identity);
                assert!(payload == base64(s(&call["out"])), "{test}");
                assert!(
                    emitter.serialize(&state, now, &alerts, &identity) == payload,
                    "{test}"
                );
            }
            "representing" => {
                let chosen: Vec<String> =
                    representing(&state).iter().map(|v| v.to_string()).collect();
                let expected: Vec<&str> =
                    call["out"].as_array().expect("out").iter().map(s).collect();
                assert_eq!(chosen, expected, "{test}");
            }
            other => panic!("unknown op {other}"),
        }
    }
    assert!(tests.len() >= 10, "{tests:?}");
}

// --- alerts -----------------------------------------------------------------------------------

fn texts(value: &Value) -> BTreeMap<String, String> {
    value
        .as_object()
        .expect("texts")
        .iter()
        .map(|(k, v)| (k.clone(), s(v).to_owned()))
        .collect()
}

fn alert_row(value: &Value) -> AlertRow {
    let opt_text = |v: &Value| v.as_str().map(str::to_owned);
    AlertRow {
        source: text(&value["source"]).expect("source"),
        alert_id: text(&value["alert_id"]).expect("id"),
        cause: text(&value["cause"]).expect("cause"),
        effect: text(&value["effect"]).expect("effect"),
        severity: text(&value["severity"]).expect("severity"),
        periods: value["periods"]
            .as_array()
            .expect("periods")
            .iter()
            .map(|p| AlertPeriod {
                start: opt_text(&p["start"]),
                end: opt_text(&p["end"]),
            })
            .collect(),
        header: texts(&value["header"]),
        description: texts(&value["description"]),
        url: texts(&value["url"]),
        cause_detail: texts(&value["cause_detail"]),
        effect_detail: texts(&value["effect_detail"]),
        entities: value.get("entities").cloned().unwrap_or(Value::Null),
        details: value.get("details").cloned().unwrap_or(Value::Null),
        targets: value["targets"]
            .as_array()
            .expect("targets")
            .iter()
            .map(|t| AlertTarget {
                kind: s(&t["kind"]).to_owned(),
                id: s(&t["id"]).to_owned(),
                service_date: opt_text(&t["service_date"]),
            })
            .collect(),
    }
}

#[test]
fn alert_entities_are_byte_identical_and_end_as_python_says() {
    let data = common::json("publish/alerts.json");
    let rows: Vec<AlertRow> = data["rows"]
        .as_array()
        .expect("rows")
        .iter()
        .map(alert_row)
        .collect();
    let times: Vec<Instant> = data["times"]
        .as_array()
        .expect("times")
        .iter()
        .map(|t| Instant::from_isoformat(s(t)).expect("a time"))
        .collect();
    for (n, row) in rows.iter().enumerate() {
        let expected = base64(s(&data["entities"][n]));
        assert!(
            alert_entity(row).expect("encodes") == expected,
            "{}",
            row.alert_id
        );
        for (t, now) in times.iter().enumerate() {
            assert_eq!(
                over(row, *now).expect("periods read"),
                data["over"][n][t].as_bool().expect("a bool"),
                "{} at {now}",
                row.alert_id
            );
        }
    }
    // The feed's entities: in row order, the ones over left out, cached while unchanged.
    let now = times[3];
    let mut ended = rows[0].clone();
    ended.alert_id = "ended".into();
    ended.periods = vec![AlertPeriod {
        start: None,
        end: Some(now.isoformat()),
    }];
    let rows: Vec<AlertRow> = rows.into_iter().chain([ended]).collect();
    let mut feed = AlertEntities::new();
    feed.load(BTreeMap::from([(Feed::Jdf, rows.clone())]));
    let first = feed.entities(Feed::Jdf, now).expect("entities");
    let live: Vec<Vec<u8>> = rows
        .iter()
        .filter(|r| !over(r, now).unwrap())
        .map(|r| alert_entity(r).unwrap())
        .collect();
    assert!(first.iter().map(|e| e.to_vec()).collect::<Vec<_>>() == live);
    assert!(first.len() < rows.len() && feed.cached() == first.len());
    let again = feed.entities(Feed::Jdf, now).expect("entities");
    assert!(Arc::ptr_eq(&first[0], &again[0]));
    feed.load(BTreeMap::from([(Feed::Jdf, rows[..1].to_vec())]));
    assert!(feed.cached() <= 1 && feed.entities(Feed::Czptt, now).unwrap().is_empty());
    let mut bad = rows[0].clone();
    bad.cause = "NOT_A_CAUSE".into();
    assert!(alert_entity(&bad).is_err());
}

#[test]
fn the_state_reader_reads_journeys() {
    let j = journey(
        &json!({"feed": "czptt", "namespace": "n", "key": "k", "service_date": "2026-10-05"}),
    )
    .expect("a journey");
    assert_eq!(j.service_date, Date::ymd(2026, 10, 5));
    assert!(state_json::float(&json!("-inf")).unwrap().is_infinite());
}
