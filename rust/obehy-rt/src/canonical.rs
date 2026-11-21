//! The canonical JSON of the compare harness: port of `realtime/canonical.py`, the
//! byte-for-byte contract between `obehy rt compare` and `obehy-rt compare` (read the Python
//! module's docstring). Objects with keys sorted by code point and compact separators; strings
//! as `json.dumps(..., ensure_ascii=False)`; floats rounded by the key they are under (`lat`,
//! `lon`, `latitude`, `longitude` 7 decimals; `*_m`, `odometer` 2; else 3), trailing zeros and
//! a trailing `.` stripped, `-0` as `0`; times as UTC `...Z` (`times.utc_text`).

use std::fmt::Write as _;

use obehy_core::py::json::PyJson;
use obehy_core::py::num::py_float_repr;
use obehy_core::time::Instant;
use serde_json::Value;

/// A JSON-ready value.
#[derive(Debug, Clone, PartialEq)]
pub enum C {
    Null,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    List(Vec<C>),
    Obj(Vec<(String, C)>),
}

impl C {
    pub fn str(s: impl Into<String>) -> C {
        C::Str(s.into())
    }

    pub fn obj<const N: usize>(pairs: [(&str, C); N]) -> C {
        C::Obj(pairs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
    }

    /// `times.utc_text`, or null.
    pub fn time(at: Option<Instant>) -> C {
        at.map_or(C::Null, |at| C::Str(utc_text(at)))
    }

    pub fn opt_str(s: Option<&str>) -> C {
        s.map_or(C::Null, |s| C::Str(s.to_owned()))
    }

    /// From what `model_json` makes.
    pub fn from_py(value: &PyJson) -> C {
        match value {
            PyJson::Null => C::Null,
            PyJson::Bool(b) => C::Bool(*b),
            PyJson::Int(n) => C::Int(i128::from(*n)),
            PyJson::Float(x) => C::Float(*x),
            PyJson::Str(s) => C::Str(s.clone()),
            PyJson::List(items) => C::List(items.iter().map(C::from_py).collect()),
            PyJson::Object(pairs) => C::Obj(
                pairs
                    .iter()
                    .map(|(k, v)| (k.clone(), C::from_py(v)))
                    .collect(),
            ),
        }
    }

    /// From a JSON value read from an archive index line.
    pub fn from_json(value: &Value) -> C {
        match value {
            Value::Null => C::Null,
            Value::Bool(b) => C::Bool(*b),
            Value::Number(n) => n
                .as_i64()
                .map(|i| C::Int(i128::from(i)))
                .or_else(|| n.as_u64().map(|u| C::Int(i128::from(u))))
                .unwrap_or_else(|| C::Float(n.as_f64().unwrap_or(f64::NAN))),
            Value::String(s) => C::Str(s.clone()),
            Value::Array(items) => C::List(items.iter().map(C::from_json).collect()),
            Value::Object(map) => C::Obj(
                map.iter()
                    .map(|(k, v)| (k.clone(), C::from_json(v)))
                    .collect(),
            ),
        }
    }
}

/// `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`.
pub fn utc_text(at: Instant) -> String {
    let utc = at.utc();
    let (h, m, s, micro) = utc.hms_micro();
    let mut text = format!("{}T{h:02}:{m:02}:{s:02}", utc.date().isoformat());
    if micro != 0 {
        write!(text, ".{micro:06}").expect("writing to a string");
    }
    text.push('Z');
    text
}

/// How many decimals a float under `key` keeps.
pub fn decimals(key: Option<&str>) -> usize {
    match key {
        Some("lat" | "lon" | "latitude" | "longitude") => 7,
        Some(k) if k.ends_with("_m") || k == "odometer" => 2,
        _ => 3,
    }
}

/// A float as canonical text; panics on NaN and infinities (Python raises).
pub fn number(value: f64, places: usize) -> String {
    assert!(value.is_finite(), "{value} has no canonical JSON form");
    let mut text = format!("{value:.places$}");
    if text.contains('.') {
        while text.ends_with('0') {
            text.pop();
        }
        if text.ends_with('.') {
            text.pop();
        }
    }
    if text == "-0" { "0".to_owned() } else { text }
}

fn write_string(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => write!(out, "\\u{:04x}", c as u32).expect("writing"),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write(out: &mut String, value: &C, key: Option<&str>, exact: bool) {
    match value {
        C::Null => out.push_str("null"),
        C::Bool(true) => out.push_str("true"),
        C::Bool(false) => out.push_str("false"),
        C::Int(n) => write!(out, "{n}").expect("writing"),
        C::Float(x) if exact => {
            assert!(x.is_finite(), "{x} has no canonical JSON form");
            out.push_str(&py_float_repr(*x));
        }
        C::Float(x) => out.push_str(&number(*x, decimals(key))),
        C::Str(s) => write_string(out, s),
        C::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(out, item, key, exact);
            }
            out.push(']');
        }
        C::Obj(pairs) => {
            let mut sorted: Vec<&(String, C)> = pairs.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (k, v)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, k);
                out.push(':');
                write(out, v, Some(k), exact);
            }
            out.push('}');
        }
    }
}

/// Record kinds whose floats are written exact (Python's `repr`), not rounded by key.
pub const EXACT_KINDS: [&str; 1] = ["journey_sample"];

/// One record: its canonical text and a newline (`canonical.line`).
pub fn line(record: &C) -> String {
    let exact = match record {
        C::Obj(fields) => fields.iter().any(|(k, v)| {
            k == "kind" && matches!(v, C::Str(kind) if EXACT_KINDS.contains(&kind.as_str()))
        }),
        _ => false,
    };
    let mut out = String::new();
    write(&mut out, record, None, exact);
    out.push('\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_round_by_key_and_keys_sort() {
        assert_eq!(number(50.081_234_567_8, 7), "50.0812346");
        assert_eq!(number(90.0, 3), "90");
        assert_eq!(number(-0.0001, 3), "0");
        assert_eq!(number(0.125, 2), "0.12");
        let record = C::obj([
            ("lat", C::Float(50.0)),
            ("b", C::List(vec![C::Float(1.23456), C::str("ž\n")])),
            ("a_m", C::Float(1.005)),
        ]);
        assert_eq!(
            line(&record),
            "{\"a_m\":1,\"b\":[1.235,\"ž\\n\"],\"lat\":50}\n"
        );
        assert_eq!(
            utc_text(Instant::from_micros(1_500_000)),
            "1970-01-01T00:00:01.500000Z"
        );
    }
}
