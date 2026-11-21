//! The values a mapping computes per entity, and Python's `==` and `str()` on them.

use indexmap::IndexMap;
use obehy_core::config::decode::{Match, Scalar};
use obehy_core::py::num::py_float_repr;
use obehy_core::py::text::{PyInt, int_from_str};
use obehy_core::time::{Date, Instant};

use crate::error::PayloadError;
use crate::reader::json::Json;

/// One computed value.
#[derive(Debug, Clone, PartialEq)]
pub enum Val {
    /// A JSON value kept as is (`as = "raw"`).
    Json(Json),
    Int(PyInt),
    Float(f64),
    Bool(bool),
    Text(String),
    Instant(Instant),
    Date(Date),
    /// A coordinate pair.
    Pair(f64, f64),
    /// Codes (SR70), in order.
    Codes(Vec<String>),
    /// Labelled texts (a popup's), last duplicate wins.
    Map(IndexMap<String, String>),
    /// A record of a kind with its fields.
    Record {
        kind: String,
        fields: Vec<String>,
    },
}

impl Val {
    pub fn int(n: i128) -> Val {
        Val::Int(PyInt::Small(n))
    }

    pub fn as_instant(&self) -> Option<Instant> {
        match self {
            Val::Instant(at) => Some(*at),
            _ => None,
        }
    }

    pub fn as_text(&self) -> Option<&str> {
        match self {
            Val::Text(t) | Val::Json(Json::Str(t)) => Some(t),
            _ => None,
        }
    }

    /// A field of a map or record (`label.Linka`, `rec.11`, `rec.kind`).
    pub fn field(&self, name: &str) -> Option<Val> {
        match self {
            Val::Map(map) => map.get(name).cloned().map(Val::Text),
            Val::Record { kind, fields } => {
                if name == "kind" {
                    Some(Val::Text(kind.clone()))
                } else {
                    let index: usize = name.parse().ok()?;
                    fields.get(index).cloned().map(Val::Text)
                }
            }
            Val::Json(j) => j.get(name).cloned().map(Val::Json),
            _ => None,
        }
    }

    /// The value as the JSON a coercion reads.
    pub fn to_json(&self) -> Json {
        match self {
            Val::Json(j) => j.clone(),
            Val::Text(t) => Json::Str(t.clone()),
            Val::Int(PyInt::Small(n)) => {
                i64::try_from(*n).map_or_else(|_| Json::BigInt(n.to_string().into()), Json::Int)
            }
            Val::Int(PyInt::Big(t)) => Json::BigInt(t.as_str().into()),
            Val::Float(x) => Json::Float(*x),
            Val::Bool(b) => Json::Bool(*b),
            _ => Json::Null,
        }
    }

    /// Python's `str(value)` in a template (`f"{value}"`).
    pub fn text(&self) -> String {
        match self {
            Val::Text(t) => t.clone(),
            Val::Int(n) => n.text(),
            Val::Float(x) => float_str(*x),
            Val::Bool(true) => "True".into(),
            Val::Bool(false) => "False".into(),
            Val::Json(j) => match j {
                Json::Float(x) => float_str(*x),
                other => other.py_str(),
            },
            Val::Instant(at) => at.isoformat(),
            Val::Date(d) => d.isoformat(),
            Val::Pair(x, y) => format!("({}, {})", float_str(*x), float_str(*y)),
            Val::Codes(c) => format!("{c:?}"),
            Val::Map(m) => format!("{m:?}"),
            Val::Record { kind, .. } => kind.clone(),
        }
    }

    /// Python's `int(value)` in a template (`f"{int(value):06d}"`): an error where Python's
    /// ValueError escapes the connector (the whole payload).
    pub fn to_int(&self) -> Result<PyInt, PayloadError> {
        match self {
            Val::Int(n) => Ok(n.clone()),
            Val::Bool(b) => Ok(PyInt::Small(i128::from(*b))),
            Val::Float(x) => float_int(*x),
            Val::Text(t) => int_from_str(t).ok_or_else(|| PayloadError::int_literal(t)),
            Val::Json(j) => match j {
                Json::Int(n) => Ok(PyInt::Small(i128::from(*n))),
                Json::BigInt(t) => Ok(PyInt::Big(t.to_string())),
                Json::Bool(b) => Ok(PyInt::Small(i128::from(*b))),
                Json::Float(x) => float_int(*x),
                Json::Str(t) => int_from_str(t).ok_or_else(|| PayloadError::int_literal(t)),
                _ => Err(PayloadError {
                    class: "TypeError",
                    message:
                        "int() argument must be a string, a bytes-like object or a real number"
                            .into(),
                }),
            },
            _ => Err(PayloadError {
                class: "TypeError",
                message: "int() argument must be a string, a bytes-like object or a real number"
                    .into(),
            }),
        }
    }
}

/// `int(x)` of a float: truncated; NaN and infinities raise.
pub fn float_int(x: f64) -> Result<PyInt, PayloadError> {
    if x.is_nan() {
        return Err(PayloadError::value("cannot convert float NaN to integer"));
    }
    if x.is_infinite() {
        return Err(PayloadError {
            class: "OverflowError",
            message: "cannot convert float infinity to integer".into(),
        });
    }
    let t = x.trunc();
    if t.abs() < 1e38 {
        return Ok(PyInt::Small(t as i128));
    }
    // Exact decimal text of a large whole float.
    let text = format!("{t:.0}");
    Ok(PyInt::from_digits(
        text.starts_with('-'),
        text.trim_start_matches('-'),
    ))
}

/// `str(x)` of a float: its repr, with `nan` and `inf`.
pub fn float_str(x: f64) -> String {
    if x.is_nan() {
        "nan".into()
    } else if x.is_infinite() {
        if x > 0.0 { "inf".into() } else { "-inf".into() }
    } else {
        py_float_repr(x)
    }
}

/// A number as Python compares it: ints exactly, floats as floats.
enum Num {
    Int(i128),
    Float(f64),
}

fn num_eq(a: &Num, b: &Num) -> bool {
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => x == y,
        (Num::Float(x), Num::Float(y)) => x == y,
        (Num::Int(i), Num::Float(f)) | (Num::Float(f), Num::Int(i)) => {
            f.is_finite() && f.fract() == 0.0 && f.abs() < 1.7e38 && *f as i128 == *i
        }
    }
}

fn val_num(v: &Val) -> Option<Num> {
    match v {
        Val::Int(PyInt::Small(n)) => Some(Num::Int(*n)),
        Val::Float(x) => Some(Num::Float(*x)),
        Val::Bool(b) => Some(Num::Int(i128::from(*b))),
        Val::Json(Json::Int(n)) => Some(Num::Int(i128::from(*n))),
        Val::Json(Json::Float(x)) => Some(Num::Float(*x)),
        Val::Json(Json::Bool(b)) => Some(Num::Int(i128::from(*b))),
        _ => None,
    }
}

/// Python's `value == scalar`.
pub fn py_eq(value: &Val, scalar: &Scalar) -> bool {
    let other = match scalar {
        Scalar::Bool(b) => Num::Int(i128::from(*b)),
        Scalar::Int(n) => Num::Int(i128::from(*n)),
        Scalar::Float(x) => Num::Float(*x),
        Scalar::Text(t) => return value.as_text() == Some(t.as_str()),
    };
    val_num(value).is_some_and(|n| num_eq(&n, &other))
}

/// Whether an (optional) value matches: absent matches nothing.
pub fn matches(value: Option<&Val>, m: &Match) -> bool {
    value.is_some_and(|v| m.scalars().iter().any(|s| py_eq(v, s)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn equality_is_pythons() {
        let one = Val::int(1);
        assert!(py_eq(&one, &Scalar::Float(1.0)) && py_eq(&one, &Scalar::Bool(true)));
        assert!(py_eq(&Val::Json(Json::Bool(true)), &Scalar::Int(1)));
        assert!(!py_eq(&Val::Json(Json::Bool(true)), &Scalar::Int(2)));
        assert!(py_eq(&Val::Float(2.0), &Scalar::Int(2)));
        assert!(!py_eq(&Val::Text("1".into()), &Scalar::Int(1)));
        assert!(py_eq(
            &Val::Json(Json::Str("H".into())),
            &Scalar::Text("H".into())
        ));
        assert!(!py_eq(&Val::Json(Json::Null), &Scalar::Int(0)));
        assert!(!matches(None, &Match::One(Scalar::Int(0))));
    }

    #[test]
    fn templates_read_ints_as_python() {
        assert_eq!(Val::Text("0105".into()).to_int().unwrap().text(), "105");
        assert_eq!(Val::int(-5).to_int().unwrap().zero_padded(6), "-00005");
        assert_eq!(
            Val::Text("²".into()).to_int().unwrap_err().class,
            "ValueError"
        );
        assert_eq!(Val::Float(2.0).text(), "2.0");
        assert_eq!(Val::Bool(true).text(), "True");
    }
}
