//! The connector cases (`tests/fixtures/cases.json`, from the Python worker's connector tests
//! plus extra payloads): every case decoded and compared with its expected result, facts and
//! filtered bodies as JSON values (numbers to 1e-9), or with "an error" where one is expected.
//! A new case is added to the fixture by hand.

use std::path::PathBuf;

use obehy_core::alerts::{Alert, Period};
use obehy_core::compositions::{Unit, UpstreamRequest, ViewCall};
use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::model::Observation;
use obehy_core::model::json::facts_to_json;
use obehy_core::time::{Instant, TimeDelta};
use obehy_sources::{Decoders, PayloadError, arriva_express, cd, dpmd};
use serde_json::{Value, json};

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fixture(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("JSON")
}

fn decoders() -> Decoders {
    let sources = manifest::load_sources(
        &repo().join("src/obehy/data/realtime/sources"),
        &RECORDER_FILTERS,
    )
    .expect("manifests");
    let package = repo().join("src/obehy");
    let files = |name: &str| std::fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    Decoders::new(&sources, &files).expect("decoders compile")
}

fn bytes(case: &Value, key: &str) -> Vec<u8> {
    let text = case[key].as_str().expect("base64");
    base64_decode(text)
}

fn base64_decode(text: &str) -> Vec<u8> {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let mut buffer = 0_u32;
    let mut bits = 0;
    for c in text.bytes() {
        if c == b'=' {
            break;
        }
        let v = ALPHABET.iter().position(|&a| a == c).expect("base64") as u32;
        buffer = (buffer << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buffer >> bits) as u8);
            buffer &= (1 << bits) - 1;
        }
    }
    out
}

fn error_value(e: &PayloadError) -> Value {
    json!({"message": e.message})
}

fn observations(found: &[Observation]) -> Value {
    Value::Array(
        found
            .iter()
            .map(|o| {
                json!({
                    "source": o.source,
                    "channel": o.channel,
                    "item": o.raw.item,
                    "sha256": o.raw.sha256,
                    "feed": o.feed.as_str(),
                    "received_at": o.received_at.isoformat(),
                    "observed_at": o.observed_at.map(Instant::isoformat),
                    "decoder_version": o.decoder_version,
                    "facts": facts_to_json(&o.facts),
                })
            })
            .collect(),
    )
}

fn texts(t: &[(String, String)]) -> Value {
    Value::Array(t.iter().map(|(a, b)| json!([a, b])).collect())
}

fn period(p: &Period) -> Value {
    json!({"start": p.start.map(Instant::isoformat), "end": p.end.map(Instant::isoformat)})
}

fn alerts(found: &[Alert]) -> Value {
    Value::Array(
        found
            .iter()
            .map(|a| {
                json!({
                    "source": a.source,
                    "channel": a.channel,
                    "alert_id": a.alert_id,
                    "cause": a.cause,
                    "effect": a.effect,
                    "severity": a.severity,
                    "periods": a.periods.iter().map(period).collect::<Vec<_>>(),
                    "header": texts(&a.header),
                    "description": texts(&a.description),
                    "url": texts(&a.url),
                    "cause_detail": texts(&a.cause_detail),
                    "effect_detail": texts(&a.effect_detail),
                    "entities": a.entities.iter().map(|e| json!({
                        "kind": e.kind,
                        "namespace": e.namespace,
                        "identifier": e.identifier,
                        "service_date": e.service_date.map(|d| d.isoformat()),
                    })).collect::<Vec<_>>(),
                    "details": texts(&a.details),
                })
            })
            .collect(),
    )
}

fn units(found: &[Unit]) -> Value {
    Value::Array(
        found
            .iter()
            .map(|u| {
                json!({
                    "sequence": u.sequence,
                    "kind": u.kind,
                    "role": u.role,
                    "vehicle_type": u.vehicle_type,
                    "coach_number": u.coach_number,
                    "classes": u.classes,
                    "features": u.features,
                    "texts": texts(&u.texts),
                    "destination": u.destination,
                    "details": texts(&u.details),
                })
            })
            .collect(),
    )
}

fn outcome<T>(found: Result<T, PayloadError>, show: impl FnOnce(&T) -> Value) -> (String, Value) {
    match found {
        Ok(v) => ("result".into(), show(&v)),
        Err(e) => ("error".into(), error_value(&e)),
    }
}

fn dummy_request() -> UpstreamRequest {
    UpstreamRequest {
        cache_key: String::new(),
        params: Vec::new(),
        start: ViewCall {
            ordinal: 0,
            location_id: String::new(),
            visit_n: 1,
            passenger: true,
            left: false,
            keys: Default::default(),
        },
    }
}

#[test]
fn every_case_decodes_as_expected() {
    let decoders = decoders();
    let cases = fixture("cases.json");
    let mut failures = Vec::new();
    for (n, case) in cases.as_array().expect("cases").iter().enumerate() {
        let kind = case["kind"].as_str().expect("kind");
        let body = bytes(case, "body");
        let (key, got) = match kind {
            "facts" => {
                let request = match &case["request"] {
                    Value::Null => None,
                    other => Some(other.clone()),
                };
                outcome(
                    decoders.decode(
                        case["source"].as_str().unwrap(),
                        case["channel"].as_str().unwrap(),
                        &body,
                        case["sha256"].as_str().unwrap(),
                        Instant::from_micros(case["received_at"].as_i64().unwrap()),
                        TimeDelta::from_micros(case["skew"].as_i64().unwrap()),
                        request.as_ref(),
                    ),
                    |o| observations(o),
                )
            }
            "filter" => {
                let filtered = match case["filter"].as_str().unwrap() {
                    "dpmd" => dpmd::recorded(&body),
                    _ => arriva_express::recorded(&body),
                };
                outcome(filtered, |f| {
                    let body: Value = serde_json::from_slice(&f.body).expect("JSON");
                    json!({"body": body, "kept": f.kept, "dropped": f.dropped})
                })
            }
            "alerts" => {
                let at = Instant::from_micros(case["received_at"].as_i64().unwrap());
                let channel = case["channel"].as_str().unwrap();
                outcome(decoders.decode_alerts("pid", channel, &body, at), |a| {
                    alerts(a)
                })
            }
            "composition" => outcome(cd::decode(&body, &dummy_request()), |u| units(u)),
            // Python's own `json` module semantics: not a connector's.
            "json" => continue,
            other => panic!("unknown case kind {other}"),
        };
        let expected = expected(case, kind);
        let same = match (key.as_str(), case.get("error")) {
            ("error", Some(_)) => true,
            ("result", None) => close(&expected, &got),
            _ => false,
        };
        if !same {
            let wanted = case
                .get("result")
                .or(case.get("error"))
                .cloned()
                .unwrap_or_default();
            failures.push(format!(
                "case {n} ({kind} {}): expected {}\n  got {key} {}",
                case.get("source")
                    .or(case.get("filter"))
                    .or(case.get("channel"))
                    .unwrap_or(&Value::Null),
                shorten(&wanted),
                shorten(&got)
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.as_array().unwrap().len(),
        failures.join("\n")
    );
}

fn shorten(v: &Value) -> String {
    let t = v.to_string();
    if t.chars().count() > 1500 {
        t.chars().take(1500).collect::<String>() + "..."
    } else {
        t
    }
}

/// The case's expected value in the shape this test builds: stored facts and filtered bodies
/// parsed.
fn expected(case: &Value, kind: &str) -> Value {
    let mut wanted = case.get("result").cloned().unwrap_or(Value::Null);
    match kind {
        "facts" => {
            for o in wanted.as_array_mut().into_iter().flatten() {
                o["facts"] = serde_json::from_str(o["facts"].as_str().expect("facts text"))
                    .expect("facts JSON");
            }
        }
        "filter" if wanted.is_object() => {
            let body = base64_decode(wanted["body"].as_str().expect("base64"));
            wanted["body"] = serde_json::from_slice(&body).expect("JSON");
        }
        _ => {}
    }
    wanted
}

/// Equal JSON values, numbers within 1e-9.
fn close(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            (x - y).abs() <= 1e-9 * x.abs().max(1.0)
        }
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(a, b)| close(a, b))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| close(v, w)))
        }
        _ => a == b,
    }
}
