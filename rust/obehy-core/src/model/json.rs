//! Facts as JSON for `rt.observation.facts`: port of `realtime/model_json.py`, versioned by
//! [`FACT_SCHEMA_VERSION`].
//!
//! [`facts_json_text`] is byte-identical to `json.dumps(facts_to_json(facts),
//! separators=(",", ":"))`, what `emit/db.py` stores; [`facts_from_json`] reads what either
//! side wrote, coercing values as Python's `str()`, `float()`, `int()` and `bool()` do.
//! Python accepts any string as a delay reference or platform label (a `cast`); here an
//! unknown one is a [`FactSchemaError`].

use serde_json::Value;

use super::facts::*;
use crate::py::json::{DumpOptions, PyJson, dumps, py_bool, py_float, py_int, py_str};
use crate::time::{Date, Instant};

/// Stored facts that this code cannot read (another schema version or a bad shape).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct FactSchemaError(pub String);

fn error(message: impl Into<String>) -> FactSchemaError {
    FactSchemaError(message.into())
}

fn iso(value: Option<Instant>) -> PyJson {
    PyJson::opt(value, |at| PyJson::str(at.isoformat()))
}

fn opt_str(value: Option<&String>) -> PyJson {
    PyJson::opt(value, |text| PyJson::str(text.clone()))
}

/// `fact_to_json`.
pub fn fact_to_json(fact: &Fact) -> PyJson {
    let s = |text: &str| PyJson::str(text);
    match fact {
        Fact::VehicleKey(f) => {
            PyJson::object([("t", s("vehicle_key")), ("id", s(&f.source_vehicle_id))])
        }
        Fact::TripKey(f) => PyJson::object([
            ("t", s("trip_key")),
            ("ns", s(&f.namespace)),
            ("key", s(&f.key)),
        ]),
        Fact::LineRef(f) => PyJson::object([
            ("t", s("line_ref")),
            ("ns", s(&f.namespace)),
            ("value", s(&f.value)),
        ]),
        Fact::Destination(f) => PyJson::object([("t", s("destination")), ("name", s(&f.name))]),
        Fact::NextStopName(f) => PyJson::object([("t", s("next_stop_name")), ("name", s(&f.name))]),
        Fact::Position(f) => PyJson::object([
            ("t", s("position")),
            ("lat", PyJson::Float(f.lat)),
            ("lon", PyJson::Float(f.lon)),
            ("bearing", PyJson::opt(f.bearing, PyJson::Float)),
        ]),
        Fact::Delay(f) => PyJson::object([
            ("t", s("delay")),
            ("s", PyJson::Int(f.seconds)),
            ("ref", s(f.reference.as_str())),
        ]),
        Fact::SourceState(f) => PyJson::object([("t", s("source_state")), ("code", s(&f.code))]),
        Fact::SourceDeparture(f) => PyJson::object([
            ("t", s("source_departure")),
            ("at", PyJson::str(f.at.isoformat())),
        ]),
        Fact::ServiceDay(f) => PyJson::object([
            ("t", s("service_day")),
            ("day", PyJson::str(f.day.isoformat())),
        ]),
        Fact::PointEvent(f) => PyJson::object([
            ("t", s("point_event")),
            ("name", s(&f.name)),
            (
                "codes",
                PyJson::List(f.codes.iter().map(|c| s(c)).collect()),
            ),
            ("scheduled", iso(f.scheduled)),
            ("lo", PyJson::str(f.actual.lo.isoformat())),
            ("hi", PyJson::str(f.actual.hi.isoformat())),
            ("standing", PyJson::Bool(f.standing)),
        ]),
        Fact::NextPoint(f) => PyJson::object([
            ("t", s("next_point")),
            ("name", s(&f.name)),
            ("sr70", opt_str(f.sr70.as_ref())),
        ]),
        Fact::NextStopPrediction(f) => PyJson::object([
            ("t", s("next_stop_prediction")),
            ("sr70", s(&f.sr70)),
            ("scheduled", iso(f.scheduled)),
            ("predicted", iso(f.predicted)),
        ]),
        Fact::TripStatus(f) => PyJson::object([
            ("t", s("trip_status")),
            ("replacement_bus", PyJson::Bool(f.replacement_bus)),
            ("diverted", PyJson::Bool(f.diverted)),
        ]),
        Fact::TripCancelled(_) => PyJson::object([("t", s("trip_cancelled"))]),
        Fact::VehicleAttribute(f) => PyJson::object([
            ("t", s("vehicle_attribute")),
            ("a", s(&f.attribute)),
            ("value", s(&f.value)),
        ]),
        Fact::PlatformAssignment(f) => PyJson::object([
            ("t", s("platform")),
            ("station", s(&f.station)),
            ("kind", s(f.kind.as_str())),
            ("scheduled", PyJson::str(f.scheduled.isoformat())),
            ("value", s(&f.value)),
            ("label", s(f.label.as_str())),
        ]),
    }
}

/// `facts_to_json`: `{"v": 5, "facts": [...]}`.
pub fn facts_to_json(facts: &[Fact]) -> PyJson {
    PyJson::object([
        ("v", PyJson::Int(FACT_SCHEMA_VERSION)),
        (
            "facts",
            PyJson::List(facts.iter().map(fact_to_json).collect()),
        ),
    ])
}

/// The stored text: `json.dumps(facts_to_json(facts), separators=(",", ":"))`.
pub fn facts_json_text(facts: &[Fact]) -> String {
    dumps(&facts_to_json(facts), DumpOptions::COMPACT)
}

fn field<'a>(data: &'a Value, key: &str) -> Result<&'a Value, FactSchemaError> {
    data.get(key)
        .ok_or_else(|| error(format!("missing {key:?}")))
}

fn text(data: &Value, key: &str) -> Result<String, FactSchemaError> {
    field(data, key).map(py_str)
}

/// `_text`: None stays None (also when absent).
fn opt_text(data: &Value, key: &str) -> Option<String> {
    data.get(key).filter(|v| !v.is_null()).map(py_str)
}

fn number(data: &Value, key: &str) -> Result<f64, FactSchemaError> {
    py_float(field(data, key)?).ok_or_else(|| error(format!("{key:?} is not a number")))
}

fn at(value: &Value) -> Result<Instant, FactSchemaError> {
    Instant::from_isoformat(&py_str(value)).map_err(|e| error(e.0))
}

fn opt_at(data: &Value, key: &str) -> Result<Option<Instant>, FactSchemaError> {
    match data.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => at(value).map(Some),
    }
}

/// `fact_from_json`.
pub fn fact_from_json(data: &Value) -> Result<Fact, FactSchemaError> {
    let kind = data.get("t").and_then(Value::as_str);
    Ok(match kind {
        Some("vehicle_key") => VehicleKey {
            source_vehicle_id: text(data, "id")?,
        }
        .into(),
        Some("trip_key") => TripKey {
            namespace: text(data, "ns")?,
            key: text(data, "key")?,
        }
        .into(),
        Some("line_ref") => LineRef {
            namespace: text(data, "ns")?,
            value: text(data, "value")?,
        }
        .into(),
        Some("destination") => Destination {
            name: text(data, "name")?,
        }
        .into(),
        Some("next_stop_name") => NextStopName {
            name: text(data, "name")?,
        }
        .into(),
        Some("position") => {
            let bearing = match data.get("bearing") {
                None | Some(Value::Null) => None,
                Some(value) => {
                    Some(py_float(value).ok_or_else(|| error("bearing is not a number"))?)
                }
            };
            Position {
                lat: number(data, "lat")?,
                lon: number(data, "lon")?,
                bearing,
            }
            .into()
        }
        Some("delay") => {
            let seconds =
                py_int(field(data, "s")?).ok_or_else(|| error("delay s is not an integer"))?;
            let reference = text(data, "ref")?;
            let reference = DelayReference::parse(&reference)
                .ok_or_else(|| error(format!("bad delay reference {reference:?}")))?;
            Delay { seconds, reference }.into()
        }
        Some("source_state") => SourceState {
            code: text(data, "code")?,
        }
        .into(),
        Some("source_departure") => SourceDeparture {
            at: at(field(data, "at")?)?,
        }
        .into(),
        Some("service_day") => {
            let day = Date::from_isoformat(&text(data, "day")?).map_err(|e| error(e.0))?;
            ServiceDay { day }.into()
        }
        Some("point_event") => {
            let codes = field(data, "codes")?
                .as_array()
                .ok_or_else(|| error("codes is not a list"))?
                .iter()
                .map(py_str)
                .collect();
            PointEvent {
                name: text(data, "name")?,
                codes,
                scheduled: opt_at(data, "scheduled")?,
                actual: Interval::new(at(field(data, "lo")?)?, at(field(data, "hi")?)?),
                standing: py_bool(field(data, "standing")?),
            }
            .into()
        }
        Some("next_point") => NextPoint {
            name: text(data, "name")?,
            sr70: opt_text(data, "sr70"),
        }
        .into(),
        Some("next_stop_prediction") => NextStopPrediction {
            sr70: text(data, "sr70")?,
            scheduled: opt_at(data, "scheduled")?,
            predicted: opt_at(data, "predicted")?,
        }
        .into(),
        Some("vehicle_attribute") => VehicleAttribute {
            attribute: text(data, "a")?,
            value: text(data, "value")?,
        }
        .into(),
        Some("trip_status") => TripStatus {
            replacement_bus: py_bool(field(data, "replacement_bus")?),
            diverted: py_bool(field(data, "diverted")?),
        }
        .into(),
        Some("trip_cancelled") => TripCancelled.into(),
        Some("platform") => {
            let kind = text(data, "kind")?;
            let kind = StopKind::parse(&kind)
                .ok_or_else(|| error(format!("bad platform kind {kind:?}")))?;
            let label = text(data, "label")?;
            let label = PlatformLabel::parse(&label)
                .ok_or_else(|| error(format!("bad platform label {label:?}")))?;
            PlatformAssignment {
                station: text(data, "station")?,
                kind,
                scheduled: at(field(data, "scheduled")?)?,
                value: text(data, "value")?,
                label,
            }
            .into()
        }
        _ => return Err(error(format!("unknown fact type {:?}", data.get("t")))),
    })
}

/// `facts_from_json`: any readable schema version.
pub fn facts_from_json(data: &Value) -> Result<Vec<Fact>, FactSchemaError> {
    let version = data.get("v").and_then(Value::as_f64);
    // Python's `in (4, 5)` also takes 4.0.
    if !version.is_some_and(|v| READABLE_FACT_SCHEMAS.iter().any(|r| *r as f64 == v)) {
        return Err(error(format!(
            "fact schema {:?} is not {READABLE_FACT_SCHEMAS:?}",
            data.get("v")
        )));
    }
    field(data, "facts")?
        .as_array()
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

    /// `test_foundation.test_facts_round_trip_through_json`.
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
            }
            .into(),
            NextStopName {
                name: "Teplice,Pražská".into(),
            }
            .into(),
            VehicleAttribute {
                attribute: "low_floor".into(),
                value: "true".into(),
            }
            .into(),
            TripCancelled.into(),
        ];
        let text = facts_json_text(&facts);
        assert!(text.starts_with(r#"{"v":5,"facts":[{"t":"vehicle_key","id":"405012"}"#));
        assert!(text.contains(r#""lat":50.66,"lon":14.03,"bearing":null"#));
        assert!(text.contains(r#""Teplice,Celn\u00ed""#));
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
