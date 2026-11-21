//! The values a mapping computes per entity.

use std::collections::BTreeMap;

use obehy_core::config::decode::{Match, Scalar};
use obehy_core::time::{Date, Instant};

use crate::error::PayloadError;
use crate::reader::json::{self, Value};

/// One computed value.
#[derive(Debug, Clone, PartialEq)]
pub enum Val {
    /// A JSON value kept as is (`as = "raw"`).
    Json(Value),
    Int(i64),
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
    Map(BTreeMap<String, String>),
    /// A record of a kind with its fields.
    Record {
        kind: String,
        fields: Vec<String>,
    },
}

impl Val {
    pub fn as_instant(&self) -> Option<Instant> {
        match self {
            Val::Instant(at) => Some(*at),
            _ => None,
        }
    }

    pub fn as_text(&self) -> Option<&str> {
        match self {
            Val::Text(t) | Val::Json(Value::String(t)) => Some(t),
            _ => None,
        }
    }

    /// A field of a map or record (`label.Linka`, `rec.11`).
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
    pub fn to_json(&self) -> Value {
        match self {
            Val::Json(j) => j.clone(),
            Val::Text(t) => Value::String(t.clone()),
            Val::Int(n) => Value::from(*n),
            Val::Float(x) => Value::from(*x),
            Val::Bool(b) => Value::Bool(*b),
            _ => Value::Null,
        }
    }

    /// The value in a template (`{value}`).
    pub fn text(&self) -> String {
        match self {
            Val::Text(t) => t.clone(),
            Val::Int(n) => n.to_string(),
            Val::Float(x) => x.to_string(),
            Val::Bool(b) => b.to_string(),
            Val::Json(j) => json::text(j),
            Val::Instant(at) => at.isoformat(),
            Val::Date(d) => d.isoformat(),
            Val::Pair(x, y) => format!("({x}, {y})"),
            Val::Codes(c) => format!("{c:?}"),
            Val::Map(m) => format!("{m:?}"),
            Val::Record { kind, .. } => kind.clone(),
        }
    }

    /// The value as an integer in a template (`{value:d}`, `{value:06d}`): an integer, a
    /// whole float or an integer text.
    pub fn to_int(&self) -> Result<i64, PayloadError> {
        let int = match self {
            Val::Int(n) => Some(*n),
            Val::Float(x) => float_int(*x),
            Val::Text(t) => t.trim().parse().ok(),
            Val::Json(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().and_then(float_int)),
            Val::Json(Value::String(t)) => t.trim().parse().ok(),
            _ => None,
        };
        int.ok_or_else(|| PayloadError::new(format!("not an integer: {}", self.text())))
    }

    /// The value as a number, if it is one.
    pub fn as_number(&self) -> Option<f64> {
        match self {
            Val::Int(n) => Some(*n as f64),
            Val::Float(x) => Some(*x),
            Val::Json(Value::Number(n)) => n.as_f64(),
            _ => None,
        }
    }

    /// Whether the value equals a manifest scalar: numbers by value, texts by text, booleans
    /// by truth.
    pub fn equals(&self, scalar: &Scalar) -> bool {
        match scalar {
            Scalar::Text(t) => self.as_text() == Some(t.as_str()),
            Scalar::Bool(b) => matches!(self, Val::Bool(v) | Val::Json(Value::Bool(v)) if v == b),
            Scalar::Int(n) => self.as_number() == Some(*n as f64),
            Scalar::Float(x) => self.as_number() == Some(*x),
        }
    }
}

/// A float truncated toward zero, if finite.
pub fn float_int(x: f64) -> Option<i64> {
    x.is_finite().then(|| x.trunc() as i64)
}

/// Whether an (optional) value matches: absent matches nothing.
pub fn matches(value: Option<&Val>, m: &Match) -> bool {
    value.is_some_and(|v| m.scalars().iter().any(|s| v.equals(s)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn equality() {
        let one = Val::Int(1);
        assert!(one.equals(&Scalar::Float(1.0)) && one.equals(&Scalar::Int(1)));
        assert!(!one.equals(&Scalar::Bool(true)));
        assert!(Val::Json(Value::Bool(true)).equals(&Scalar::Bool(true)));
        assert!(Val::Float(2.0).equals(&Scalar::Int(2)));
        assert!(!Val::Text("1".into()).equals(&Scalar::Int(1)));
        assert!(Val::Json(Value::String("H".into())).equals(&Scalar::Text("H".into())));
        assert!(!Val::Json(Value::Null).equals(&Scalar::Int(0)));
        assert!(!matches(None, &Match::One(Scalar::Int(0))));
    }

    #[test]
    fn template_integers() {
        assert_eq!(Val::Text("0105".into()).to_int().unwrap(), 105);
        assert_eq!(Val::Float(2.9).to_int().unwrap(), 2);
        assert!(Val::Text("x".into()).to_int().is_err());
        assert_eq!(Val::Float(2.5).text(), "2.5");
    }
}
