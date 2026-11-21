//! The canonical JSON of the output stream (`compare`, `corpus-stream`): one object per line,
//! keys sorted, floats rounded by the key they are under (`lat`, `lon`, `latitude`,
//! `longitude` 7 decimals; `*_m`, `odometer` 2; else 3) except in [`EXACT_KINDS`], and times as
//! UTC `...Z` ([`utc_text`]). Records are built as [`Value`]s with the helpers here.

use obehy_core::time::Instant;
use serde_json::{Map, Value};

/// An object from `(key, value)` pairs.
pub fn obj<const N: usize>(pairs: [(&str, Value); N]) -> Value {
    Value::Object(pairs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
}

/// A string.
pub fn text(value: impl Into<String>) -> Value {
    Value::String(value.into())
}

/// A string, or null.
pub fn opt_text(value: Option<&str>) -> Value {
    value.map_or(Value::Null, text)
}

/// [`utc_text`], or null.
pub fn time(at: Option<Instant>) -> Value {
    at.map_or(Value::Null, |at| Value::String(utc_text(at)))
}

/// `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`.
pub fn utc_text(at: Instant) -> String {
    let utc = at.utc();
    let (h, m, s, micro) = utc.hms_micro();
    let mut text = format!("{}T{h:02}:{m:02}:{s:02}", utc.date().isoformat());
    if micro != 0 {
        text.push_str(&format!(".{micro:06}"));
    }
    text.push('Z');
    text
}

/// How many decimals a float under `key` keeps.
pub fn decimals(key: Option<&str>) -> i32 {
    match key {
        Some("lat" | "lon" | "latitude" | "longitude") => 7,
        Some(k) if k.ends_with("_m") || k == "odometer" => 2,
        _ => 3,
    }
}

/// `value` rounded to `places` decimals (`-0` as `0`).
pub fn round(value: f64, places: i32) -> f64 {
    let scale = 10_f64.powi(places);
    let rounded = (value * scale).round() / scale;
    if rounded == 0.0 { 0.0 } else { rounded }
}

/// Record kinds whose floats are written exact, not rounded by key.
pub const EXACT_KINDS: [&str; 1] = ["journey_sample"];

fn canonical(value: &Value, key: Option<&str>, exact: bool) -> Value {
    match value {
        Value::Number(n) if n.is_f64() && !exact => {
            Value::from(round(n.as_f64().expect("a float"), decimals(key)))
        }
        Value::Array(items) => items.iter().map(|v| canonical(v, key, exact)).collect(),
        Value::Object(fields) => {
            let mut sorted: Vec<(&String, &Value)> = fields.iter().collect();
            sorted.sort_by_key(|(k, _)| *k);
            Value::Object(
                sorted
                    .into_iter()
                    .map(|(k, v)| (k.clone(), canonical(v, Some(k), exact)))
                    .collect::<Map<_, _>>(),
            )
        }
        other => other.clone(),
    }
}

/// One record: its canonical text and a newline.
pub fn line(record: &Value) -> String {
    let exact = record
        .get("kind")
        .and_then(Value::as_str)
        .is_some_and(|kind| EXACT_KINDS.contains(&kind));
    let mut out = canonical(record, None, exact).to_string();
    out.push('\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_round_by_key_and_keys_sort() {
        assert_eq!(round(50.081_234_567_8, 7), 50.081_234_6);
        assert_eq!(round(-0.0001, 3), 0.0);
        let record = obj([
            ("lat", Value::from(50.0)),
            ("b", Value::Array(vec![Value::from(1.23456), text("ž\n")])),
            ("a_m", Value::from(1.004)),
            ("n", Value::from(7)),
        ]);
        assert_eq!(
            line(&record),
            "{\"a_m\":1.0,\"b\":[1.235,\"ž\\n\"],\"lat\":50.0,\"n\":7}\n"
        );
        assert_eq!(
            utc_text(Instant::from_micros(1_500_000)),
            "1970-01-01T00:00:01.500000Z"
        );
    }
}
