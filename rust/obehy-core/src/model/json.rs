//! Facts as JSON for `rt.observation.facts`, versioned by [`FACT_SCHEMA_VERSION`]:
//! `{"v": 5, "facts": [{"t": "vehicle_key", "id": "405012"}, ...]}`, each fact in its serde form.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::facts::*;

/// Stored facts that this code cannot read (another schema version or a bad shape).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct FactSchemaError(pub String);

fn error(message: impl Into<String>) -> FactSchemaError {
    FactSchemaError(message.into())
}

#[derive(Serialize)]
struct Stored<'a> {
    v: i64,
    facts: &'a [Fact],
}

/// `{"v": 5, "facts": [...]}`.
pub fn facts_to_json(facts: &[Fact]) -> Value {
    serde_json::to_value(Stored {
        v: FACT_SCHEMA_VERSION,
        facts,
    })
    .expect("facts serialize")
}

/// The stored text.
pub fn facts_json_text(facts: &[Fact]) -> String {
    serde_json::to_string(&Stored {
        v: FACT_SCHEMA_VERSION,
        facts,
    })
    .expect("facts serialize")
}

/// One fact.
pub fn fact_from_json(data: &Value) -> Result<Fact, FactSchemaError> {
    Fact::deserialize(data).map_err(|e| error(e.to_string()))
}

/// Facts of any readable schema version.
pub fn facts_from_json(data: &Value) -> Result<Vec<Fact>, FactSchemaError> {
    let version = data.get("v").and_then(Value::as_i64);
    if !version.is_some_and(|v| READABLE_FACT_SCHEMAS.contains(&v)) {
        return Err(error(format!(
            "fact schema {:?} is not {READABLE_FACT_SCHEMAS:?}",
            data.get("v")
        )));
    }
    data.get("facts")
        .and_then(Value::as_array)
        .ok_or_else(|| error("facts is not a list"))?
        .iter()
        .map(fact_from_json)
        .collect()
}

/// [`facts_from_json`] of stored text.
pub fn facts_from_json_text(text: &str) -> Result<Vec<Fact>, FactSchemaError> {
    let value: Value = serde_json::from_str(text).map_err(|e| error(format!("not JSON: {e}")))?;
    facts_from_json(&value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::time::Instant;

    /// `test_foundation.test_facts_round_trip_through_json`, and every fact type.
    #[test]
    fn facts_round_trip_through_json() {
        let facts: Vec<Fact> = vec![
            VehicleKey {
                source_vehicle_id: "405012".into(),
            }
            .into(),
            TripKey {
                namespace: "cis:line_trip".into(),
                key: "582492:143".into(),
            }
            .into(),
            Position {
                lat: 50.66,
                lon: 14.03,
                bearing: None,
            }
            .into(),
            Delay {
                seconds: -60,
                reference: DelayReference::Unknown,
            }
            .into(),
            SourceState { code: "1".into() }.into(),
            SourceDeparture {
                at: Instant::from_isoformat("2026-10-10T22:54:00+00:00").unwrap(),
            }
            .into(),
            LineRef {
                namespace: "cis:line".into(),
                value: "580916".into(),
            }
            .into(),
            Destination {
                name: "Teplice,Celní".into(),
                townless: false,
            }
            .into(),
            NextStopName {
                name: "Teplice,Pražská".into(),
                scheduled: None,
                townless: false,
            }
            .into(),
            Destination {
                name: "Poruba,Opavská".into(),
                townless: true,
            }
            .into(),
            NextStopName {
                name: "Svinov,mosty".into(),
                scheduled: Some(Instant::from_isoformat("2026-10-12T05:30:00+00:00").unwrap()),
                townless: true,
            }
            .into(),
            VehicleAttribute {
                attribute: "low_floor".into(),
                value: "true".into(),
            }
            .into(),
            TripCancelled.into(),
            PointEvent {
                name: "Ústí n.L. hl.n.".into(),
                codes: vec!["5453".into()],
                scheduled: None,
                actual: Interval::new(Instant::from_micros(0), Instant::from_micros(1_000_000)),
                standing: true,
            }
            .into(),
            PlatformAssignment {
                station: "54534".into(),
                kind: StopKind::Arrival,
                scheduled: Instant::from_micros(60_000_000),
                value: "2".into(),
                label: PlatformLabel::Track,
            }
            .into(),
            ServiceDay {
                day: crate::time::Date::ymd(2026, 10, 10),
            }
            .into(),
        ];
        let text = facts_json_text(&facts);
        assert!(text.starts_with(r#"{"v":5,"facts":[{"t":"vehicle_key","id":"405012"}"#));
        assert!(text.contains(r#""lat":50.66,"lon":14.03,"bearing":null"#));
        assert!(text.contains(r#""Teplice,Celní""#));
        assert_eq!(facts_from_json_text(&text).unwrap(), facts);
        // Version 5 only dropped fact types nothing read: warm replay still reads version 4.
        let mut stored: Value = serde_json::from_str(&text).unwrap();
        stored["v"] = 4.into();
        stored["facts"].as_array_mut().unwrap().truncate(2);
        assert_eq!(facts_from_json(&stored).unwrap(), facts[..2]);
        stored["v"] = 3.into();
        assert!(facts_from_json(&stored).is_err());
    }
}
