//! Arriva Express: the recorder filter `arriva-express` (ARRIVA-Q7), port of
//! `arriva_express.recorded`. The archived body's sha256 names it, so the output is byte for
//! byte Python's: the whole GraphQL batch re-serialised (`ensure_ascii=False`, compact) with
//! only `mainType = "ARRIVA EXPRESS"` vehicles. Decoding is declarative (the manifest).

use crate::error::PayloadError;
use crate::reader::json::{self, Json, JsonError};

const EXPRESS: &str = "ARRIVA EXPRESS";

/// What a recorder filter keeps: the body to archive, vehicles kept and dropped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Filtered {
    pub body: Vec<u8>,
    pub kept: usize,
    pub dropped: usize,
}

pub fn recorded(body: &[u8]) -> Result<Filtered, PayloadError> {
    let mut document = json::loads(body).map_err(|e| match e {
        JsonError::Value(m) => PayloadError::value(m),
        other => PayloadError::payload(format!("not JSON: {}", other.message())),
    })?;
    let Json::Array(items) = &mut document else {
        return Err(PayloadError::payload(
            "expected a GraphQL batch (JSON array)",
        ));
    };
    let (mut kept, mut dropped) = (0, 0);
    for item in items.iter_mut() {
        let data = match item {
            Json::Object(o) => o.get_mut("data"),
            _ => None,
        };
        let Some(Json::Object(data)) = data else {
            return Err(PayloadError::payload("batch item without a data object"));
        };
        let Some(Json::Array(entries)) = data.get("busesCurrentLocations") else {
            return Err(PayloadError::payload(
                "data.busesCurrentLocations is not a list",
            ));
        };
        let express: Vec<Json> = entries
            .iter()
            .filter(|v| v.get("mainType").and_then(Json::as_str) == Some(EXPRESS))
            .cloned()
            .collect();
        kept += express.len();
        dropped += entries.len() - express.len();
        data.insert("busesCurrentLocations".into(), Json::Array(express));
    }
    Ok(Filtered {
        body: json::dumps_compact(&document, false).into_bytes(),
        kept,
        dropped,
    })
}
