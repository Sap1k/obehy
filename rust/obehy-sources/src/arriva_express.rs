//! Arriva Express: the recorder filter `arriva-express` (ARRIVA-Q7): the whole GraphQL batch
//! re-serialised with only `mainType = "ARRIVA EXPRESS"` vehicles. Decoding is declarative (the
//! manifest).

use crate::error::PayloadError;
use crate::reader::json::{self, Value};

const EXPRESS: &str = "ARRIVA EXPRESS";

/// What a recorder filter keeps: the body to archive, vehicles kept and dropped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Filtered {
    pub body: Vec<u8>,
    pub kept: usize,
    pub dropped: usize,
}

pub fn recorded(body: &[u8]) -> Result<Filtered, PayloadError> {
    let mut document = json::parse(body, "not JSON")?;
    let Value::Array(items) = &mut document else {
        return Err(PayloadError::new("expected a GraphQL batch (JSON array)"));
    };
    let (mut kept, mut dropped) = (0, 0);
    for item in items.iter_mut() {
        let Some(Value::Object(data)) = item.get_mut("data") else {
            return Err(PayloadError::new("batch item without a data object"));
        };
        let Some(Value::Array(entries)) = data.get("busesCurrentLocations") else {
            return Err(PayloadError::new(
                "data.busesCurrentLocations is not a list",
            ));
        };
        let express: Vec<Value> = entries
            .iter()
            .filter(|v| v.get("mainType").and_then(Value::as_str) == Some(EXPRESS))
            .cloned()
            .collect();
        kept += express.len();
        dropped += entries.len() - express.len();
        data.insert("busesCurrentLocations".into(), Value::Array(express));
    }
    Ok(Filtered {
        body: document.to_string().into_bytes(),
        kept,
        dropped,
    })
}
