//! The connector unit tests, ported: every call the Python tests make to a decoder (and the
//! extra payloads of rust/tools/gen_sources_fixtures.py), replayed here and compared whole with
//! Python's result or exception (class and text). Regenerate the fixture with
//! `uv run python rust/tools/gen_sources_fixtures.py`.

use std::path::PathBuf;

use obehy_core::alerts::{Alert, Period};
use obehy_core::compositions::{Unit, UpstreamRequest, ViewCall};
use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::model::Observation;
use obehy_core::model::json::facts_json_text;
use obehy_core::time::{Instant, TimeDelta};
use obehy_sources::reader::json::{self as pyjson, Json};
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
        &repo().join("src/obehy/realtime/sources"),
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
    json!({"class": e.class, "message": e.message})
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
                    "facts": facts_json_text(&o.facts),
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
fn every_python_case_decodes_alike() {
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
                    other => Some(pyjson::loads(other.to_string().as_bytes()).expect("request")),
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
                outcome(
                    filtered,
                    |f| json!({"body": base64_encode(&f.body), "kept": f.kept, "dropped": f.dropped}),
                )
            }
            "alerts" => {
                let at = Instant::from_micros(case["received_at"].as_i64().unwrap());
                let channel = case["channel"].as_str().unwrap();
                outcome(decoders.decode_alerts("pid", channel, &body, at), |a| {
                    alerts(a)
                })
            }
            "composition" => outcome(cd::decode(&body, &dummy_request()), |u| units(u)),
            "json" => outcome(
                pyjson::loads(&body)
                    .map_err(|e| PayloadError::payload(format!("not JSON: {}", e.message()))),
                |v| Value::String(pyjson::dumps_compact(v, true)),
            ),
            other => panic!("unknown case kind {other}"),
        };
        let expected = &case[key.as_str()];
        if expected != &got {
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

fn base64_encode(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(ALPHABET[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

#[test]
fn json_reads_and_fails_as_python() {
    let mut failures = Vec::new();
    for case in fixture("json.json").as_array().unwrap() {
        let body = bytes(case, "body");
        let got = match pyjson::loads(&body) {
            Ok(v) => json!({"ok": pyjson::dumps_compact(&v, true)}),
            Err(pyjson::JsonError::Value(m)) => {
                json!({"error": {"class": "ValueError", "message": m}})
            }
            Err(e) => {
                json!({"error": {"class": "PayloadError", "message": format!("not JSON: {}", e.message())}})
            }
        };
        let expected = match case.get("ok") {
            Some(ok) => json!({"ok": ok}),
            None => json!({"error": case["error"]}),
        };
        // Accepted divergence: Python keeps a lone surrogate in a string, Rust cannot.
        let lone = got["error"]["message"]
            .as_str()
            .is_some_and(|m| m.contains("lone surrogate"))
            && expected["ok"]
                .as_str()
                .is_some_and(|o| o.contains(r"\ud8") || o.contains(r"\udc"));
        if got != expected && !lone {
            failures.push(format!("{body:?}: expected {expected}, got {got}"));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn json_keeps_python_values() {
    let v = pyjson::loads(b"{\"a\": [1, 2.0]}").unwrap();
    assert_eq!(
        v.get("a"),
        Some(&Json::Array(vec![Json::Int(1), Json::Float(2.0)]))
    );
}
