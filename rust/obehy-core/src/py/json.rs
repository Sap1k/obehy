//! JSON as Python's `json` module writes and reads it.
//!
//! [`dumps`] produces the exact text of `json.dumps`: keys in insertion order (or sorted),
//! `float.__repr__` for floats (`1.0`, `1e-05`, `NaN`, `Infinity`), and `ensure_ascii`
//! escaping (`\u00e1`, surrogate pairs) or raw UTF-8. Reading goes through `serde_json` (with
//! `float_roundtrip`, so floats parse correctly rounded as Python's do); the `py_*` helpers
//! coerce a value as Python's `str()`, `float()`, `int()` and `bool()` would.

use std::fmt::Write as _;

use serde_json::Value;

use super::num::py_float_repr;

/// A JSON value with Python's distinctions: ints and floats apart, object keys in order.
#[derive(Debug, Clone, PartialEq)]
pub enum PyJson {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(Text),
    List(Vec<PyJson>),
    Object(Vec<(Text, PyJson)>),
}

/// A JSON string or object key: borrowed when it is a literal (a key, an enum's text), so
/// building a value allocates only for the texts that vary.
pub type Text = std::borrow::Cow<'static, str>;

impl PyJson {
    pub fn str(value: impl Into<String>) -> PyJson {
        PyJson::Str(Text::Owned(value.into()))
    }

    /// A literal string, not copied.
    pub fn lit(value: &'static str) -> PyJson {
        PyJson::Str(Text::Borrowed(value))
    }

    /// An object from owned `(key, value)` pairs, in order.
    pub fn from_pairs(pairs: Vec<(String, PyJson)>) -> PyJson {
        PyJson::Object(
            pairs
                .into_iter()
                .map(|(k, v)| (Text::Owned(k), v))
                .collect(),
        )
    }

    /// `None` for an absent value.
    pub fn opt<T>(value: Option<T>, f: impl FnOnce(T) -> PyJson) -> PyJson {
        value.map_or(PyJson::Null, f)
    }

    /// An object from `(key, value)` pairs, in order.
    pub fn object<const N: usize>(pairs: [(&'static str, PyJson); N]) -> PyJson {
        PyJson::Object(
            pairs
                .into_iter()
                .map(|(k, v)| (Text::Borrowed(k), v))
                .collect(),
        )
    }
}

/// `json.dumps` options.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DumpOptions {
    pub ensure_ascii: bool,
    pub item_separator: &'static str,
    pub key_separator: &'static str,
    pub sort_keys: bool,
}

impl DumpOptions {
    /// `json.dumps(value)`.
    pub const DEFAULT: DumpOptions = DumpOptions {
        ensure_ascii: true,
        item_separator: ", ",
        key_separator: ": ",
        sort_keys: false,
    };

    /// `json.dumps(value, separators=(",", ":"))`.
    pub const COMPACT: DumpOptions = DumpOptions {
        ensure_ascii: true,
        item_separator: ",",
        key_separator: ":",
        sort_keys: false,
    };

    #[must_use]
    pub const fn utf8(self) -> DumpOptions {
        DumpOptions {
            ensure_ascii: false,
            ..self
        }
    }

    #[must_use]
    pub const fn sorted(self) -> DumpOptions {
        DumpOptions {
            sort_keys: true,
            ..self
        }
    }
}

/// `json.dumps(value, ...)`, byte for byte.
pub fn dumps(value: &PyJson, options: DumpOptions) -> String {
    let mut out = String::new();
    write_value(&mut out, value, options);
    out
}

fn write_value(out: &mut String, value: &PyJson, options: DumpOptions) {
    match value {
        PyJson::Null => out.push_str("null"),
        PyJson::Bool(true) => out.push_str("true"),
        PyJson::Bool(false) => out.push_str("false"),
        PyJson::Int(n) => write!(out, "{n}").expect("writing to a string"),
        PyJson::Float(x) => out.push_str(&float_text(*x)),
        PyJson::Str(text) => write_string(out, text, options.ensure_ascii),
        PyJson::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(options.item_separator);
                }
                write_value(out, item, options);
            }
            out.push(']');
        }
        PyJson::Object(pairs) => {
            let mut order: Vec<&(Text, PyJson)> = pairs.iter().collect();
            if options.sort_keys {
                order.sort_by(|a, b| a.0.cmp(&b.0));
            }
            out.push('{');
            for (i, (key, item)) in order.into_iter().enumerate() {
                if i > 0 {
                    out.push_str(options.item_separator);
                }
                write_string(out, key, options.ensure_ascii);
                out.push_str(options.key_separator);
                write_value(out, item, options);
            }
            out.push('}');
        }
    }
}

/// `json`'s `floatstr`: `NaN`, `Infinity`, `-Infinity`, else `float.__repr__`.
pub fn float_text(x: f64) -> String {
    if x.is_nan() {
        "NaN".to_owned()
    } else if x == f64::INFINITY {
        "Infinity".to_owned()
    } else if x == f64::NEG_INFINITY {
        "-Infinity".to_owned()
    } else {
        py_float_repr(x)
    }
}

fn write_string(out: &mut String, text: &str, ensure_ascii: bool) {
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
            c if ensure_ascii && (c as u32) > 0x7e => {
                let mut units = [0_u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    write!(out, "\\u{unit:04x}").expect("writing");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python's `str(value)` of a decoded JSON value.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(n) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => i.to_string(),
            (None, Some(u)) => u.to_string(),
            _ => py_float_repr(n.as_f64().expect("a JSON number")),
        },
        Value::String(text) => text.clone(),
        // A list or dict's str() is its repr; facts never hold one where a string goes.
        other => other.to_string(),
    }
}

/// Python's `float(value)`: a number (a bool is 1.0 / 0.0) or a numeric string.
pub fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        Value::Number(n) => n.as_f64(),
        Value::String(text) => parse_py_float(text),
        _ => None,
    }
}

fn parse_py_float(text: &str) -> Option<f64> {
    // Rust accepts the same spellings (`1e5`, `.5`, `inf`, `nan`); Python also takes `1_0`.
    crate::time::py_strip(text).parse().ok()
}

/// Python's `int(value)`: an int, a float truncated toward zero, a bool, or an integer string.
pub fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(b) => Some(i64::from(*b)),
        Value::Number(n) => n.as_i64().or_else(|| {
            n.as_f64()
                .filter(|x| x.is_finite())
                .map(|x| x.trunc() as i64)
        }),
        Value::String(text) => crate::time::py_strip(text).replace('_', "").parse().ok(),
        _ => None,
    }
}

/// Python's `bool(value)`: truthiness.
pub fn py_bool(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|x| x != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dumps_like_python() {
        let value = PyJson::object([
            ("t", PyJson::str("destination")),
            ("name", PyJson::str("Teplice,Celní \"x\"\n\u{1F68C}\u{7f}")),
            ("lat", PyJson::Float(50.0)),
            ("tiny", PyJson::Float(1e-5)),
            ("s", PyJson::Int(-60)),
            ("none", PyJson::Null),
            (
                "l",
                PyJson::List(vec![PyJson::Bool(true), PyJson::Float(f64::NAN)]),
            ),
        ]);
        assert_eq!(
            dumps(&value, DumpOptions::COMPACT),
            r#"{"t":"destination","name":"Teplice,Celn\u00ed \"x\"\n\ud83d\ude8c\u007f","lat":50.0,"tiny":1e-05,"s":-60,"none":null,"l":[true,NaN]}"#
        );
        assert_eq!(
            dumps(&value, DumpOptions::DEFAULT.utf8().sorted()),
            "{\"l\": [true, NaN], \"lat\": 50.0, \"name\": \"Teplice,Celní \\\"x\\\"\\n\u{1F68C}\u{7f}\", \"none\": null, \"s\": -60, \"t\": \"destination\", \"tiny\": 1e-05}"
        );
    }

    #[test]
    fn coercions_follow_python() {
        let v = |text: &str| serde_json::from_str::<Value>(text).unwrap();
        assert_eq!(py_str(&v("405012")), "405012");
        assert_eq!(py_str(&v("1.0")), "1.0");
        assert_eq!(py_str(&v("true")), "True");
        assert_eq!(py_float(&v("\"50.5\"")), Some(50.5));
        assert_eq!(py_float(&v("\"abc\"")), None);
        assert_eq!(py_int(&v("-60.9")), Some(-60));
        assert!(!py_bool(&v("0")) && py_bool(&v("\"x\"")));
    }
}
