//! JSON payloads, read with serde_json (object keys in document order).

pub use serde_json::{Map, Value};

use crate::error::PayloadError;

/// A body as JSON (a UTF-8 byte order mark is skipped); `what` names the payload in the error.
pub fn parse(body: &[u8], what: &str) -> Result<Value, PayloadError> {
    let body = body.strip_prefix(b"\xef\xbb\xbf").unwrap_or(body);
    serde_json::from_slice(body).map_err(|e| PayloadError::new(format!("{what}: {e}")))
}

/// Whether a value counts as present: not null, false, zero, empty text or an empty container.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|x| x != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// A value as text: a string as itself, anything else as its JSON.
pub fn text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}
