//! A GTFS-RT `FeedMessage` as the compare harness writes it: Python's
//! `json_format.MessageToDict(message, preserving_proto_field_name=True)`. Only fields that
//! are set (proto2 presence; empty repeated fields left out), 64-bit integers as strings, enums
//! by name, `float` fields as the shortest decimal of up to 9 significant digits that reads
//! back as the same float32 (`type_checkers.ToShortestFloat`); the canonical float rule then
//! rounds them by key.

use obehy_core::publish::gtfs_rt::proto;
use prost::Message;

use crate::canonical::C;

struct Fields(Vec<(String, C)>);

impl Fields {
    fn new() -> Fields {
        Fields(Vec::new())
    }

    fn put(&mut self, name: &str, value: Option<C>) {
        if let Some(value) = value {
            self.0.push((name.to_owned(), value));
        }
    }

    fn list(&mut self, name: &str, values: Vec<C>) {
        if !values.is_empty() {
            self.0.push((name.to_owned(), C::List(values)));
        }
    }

    fn done(self) -> C {
        C::Obj(self.0)
    }
}

fn text(value: &Option<String>) -> Option<C> {
    value.as_ref().map(C::str)
}

fn int(value: Option<i64>) -> Option<C> {
    value.map(|v| C::Int(i128::from(v)))
}

fn long(value: Option<i64>) -> Option<C> {
    value.map(|v| C::Str(v.to_string()))
}

fn ulong(value: Option<u64>) -> Option<C> {
    value.map(|v| C::Str(v.to_string()))
}

fn enum_name(value: Option<i32>, names: &[(i32, &str)]) -> Option<C> {
    value.map(|v| match names.iter().find(|(n, _)| *n == v) {
        Some((_, name)) => C::str(*name),
        None => C::Int(i128::from(v)),
    })
}

/// `ToShortestFloat`: the first of `'%.{6..9}g' % value` that reads back as the same float32.
pub fn shortest_float(value: f32) -> f64 {
    let wide = f64::from(value);
    if !wide.is_finite() {
        return wide;
    }
    for precision in 6..=9 {
        let text = format!("{:.*e}", precision - 1, wide);
        let parsed: f64 = text.parse().expect("a formatted float");
        if parsed as f32 == value {
            return parsed;
        }
    }
    wide
}

fn float(value: Option<f32>) -> Option<C> {
    value.map(|v| C::Float(shortest_float(v)))
}

const TRIP_RELATIONSHIP: &[(i32, &str)] = &[
    (0, "SCHEDULED"),
    (1, "ADDED"),
    (2, "UNSCHEDULED"),
    (3, "CANCELED"),
    (5, "REPLACEMENT"),
    (6, "DUPLICATED"),
    (7, "DELETED"),
    (8, "NEW"),
];
const STOP_RELATIONSHIP: &[(i32, &str)] = &[
    (0, "SCHEDULED"),
    (1, "SKIPPED"),
    (2, "NO_DATA"),
    (3, "UNSCHEDULED"),
];
const INCREMENTALITY: &[(i32, &str)] = &[(0, "FULL_DATASET"), (1, "DIFFERENTIAL")];
const CAUSE: &[(i32, &str)] = &[
    (1, "UNKNOWN_CAUSE"),
    (2, "OTHER_CAUSE"),
    (3, "TECHNICAL_PROBLEM"),
    (4, "STRIKE"),
    (5, "DEMONSTRATION"),
    (6, "ACCIDENT"),
    (7, "HOLIDAY"),
    (8, "WEATHER"),
    (9, "MAINTENANCE"),
    (10, "CONSTRUCTION"),
    (11, "POLICE_ACTIVITY"),
    (12, "MEDICAL_EMERGENCY"),
];
const EFFECT: &[(i32, &str)] = &[
    (1, "NO_SERVICE"),
    (2, "REDUCED_SERVICE"),
    (3, "SIGNIFICANT_DELAYS"),
    (4, "DETOUR"),
    (5, "ADDITIONAL_SERVICE"),
    (6, "MODIFIED_SERVICE"),
    (7, "OTHER_EFFECT"),
    (8, "UNKNOWN_EFFECT"),
    (9, "STOP_MOVED"),
    (10, "NO_EFFECT"),
    (11, "ACCESSIBILITY_ISSUE"),
];
const SEVERITY: &[(i32, &str)] = &[
    (1, "UNKNOWN_SEVERITY"),
    (2, "INFO"),
    (3, "WARNING"),
    (4, "SEVERE"),
];

fn trip(t: &proto::TripDescriptor) -> C {
    let mut f = Fields::new();
    f.put("trip_id", text(&t.trip_id));
    f.put("start_date", text(&t.start_date));
    f.put(
        "schedule_relationship",
        enum_name(t.schedule_relationship, TRIP_RELATIONSHIP),
    );
    f.done()
}

fn vehicle(v: &proto::VehicleDescriptor) -> C {
    let mut f = Fields::new();
    f.put("id", text(&v.id));
    f.put("label", text(&v.label));
    f.done()
}

fn event(e: &proto::StopTimeEvent) -> C {
    let mut f = Fields::new();
    f.put("time", long(e.time));
    f.done()
}

fn stop_time_update(u: &proto::StopTimeUpdate) -> C {
    let mut f = Fields::new();
    f.put("stop_sequence", int(u.stop_sequence.map(i64::from)));
    f.put("arrival", u.arrival.as_ref().map(event));
    f.put("departure", u.departure.as_ref().map(event));
    f.put(
        "schedule_relationship",
        enum_name(u.schedule_relationship, STOP_RELATIONSHIP),
    );
    f.put(
        "stop_time_properties",
        u.stop_time_properties.as_ref().map(|p| {
            let mut g = Fields::new();
            g.put("assigned_stop_id", text(&p.assigned_stop_id));
            g.done()
        }),
    );
    f.done()
}

fn trip_update(t: &proto::TripUpdate) -> C {
    let mut f = Fields::new();
    f.put("trip", t.trip.as_ref().map(trip));
    f.list(
        "stop_time_update",
        t.stop_time_update.iter().map(stop_time_update).collect(),
    );
    f.put("vehicle", t.vehicle.as_ref().map(vehicle));
    f.put("timestamp", ulong(t.timestamp));
    f.done()
}

fn vehicle_position(v: &proto::VehiclePosition) -> C {
    let mut f = Fields::new();
    f.put("trip", v.trip.as_ref().map(trip));
    f.put(
        "position",
        v.position.as_ref().map(|p| {
            let mut g = Fields::new();
            g.put("latitude", float(p.latitude));
            g.put("longitude", float(p.longitude));
            g.put("bearing", float(p.bearing));
            g.done()
        }),
    );
    f.put("timestamp", ulong(v.timestamp));
    f.put("vehicle", v.vehicle.as_ref().map(vehicle));
    f.done()
}

fn translated(t: &proto::TranslatedString) -> C {
    let mut f = Fields::new();
    f.list(
        "translation",
        t.translation
            .iter()
            .map(|t| {
                let mut g = Fields::new();
                g.put("text", text(&t.text));
                g.put("language", text(&t.language));
                g.done()
            })
            .collect(),
    );
    f.done()
}

fn alert(a: &proto::Alert) -> C {
    let mut f = Fields::new();
    f.list(
        "active_period",
        a.active_period
            .iter()
            .map(|p| {
                let mut g = Fields::new();
                g.put("start", ulong(p.start));
                g.put("end", ulong(p.end));
                g.done()
            })
            .collect(),
    );
    f.list(
        "informed_entity",
        a.informed_entity
            .iter()
            .map(|e| {
                let mut g = Fields::new();
                g.put("route_id", text(&e.route_id));
                g.put("trip", e.trip.as_ref().map(trip));
                g.put("stop_id", text(&e.stop_id));
                g.done()
            })
            .collect(),
    );
    f.put("cause", enum_name(a.cause, CAUSE));
    f.put("effect", enum_name(a.effect, EFFECT));
    f.put("url", a.url.as_ref().map(translated));
    f.put("header_text", a.header_text.as_ref().map(translated));
    f.put(
        "description_text",
        a.description_text.as_ref().map(translated),
    );
    f.put("severity_level", enum_name(a.severity_level, SEVERITY));
    f.put("cause_detail", a.cause_detail.as_ref().map(translated));
    f.put("effect_detail", a.effect_detail.as_ref().map(translated));
    f.done()
}

/// `MessageToDict` of a serialized `FeedMessage`.
pub fn message_dict(bytes: &[u8]) -> Result<C, prost::DecodeError> {
    let message = proto::FeedMessage::decode(bytes)?;
    let mut f = Fields::new();
    f.put(
        "header",
        message.header.as_ref().map(|h| {
            let mut g = Fields::new();
            g.put("gtfs_realtime_version", text(&h.gtfs_realtime_version));
            g.put(
                "incrementality",
                enum_name(h.incrementality, INCREMENTALITY),
            );
            g.put("timestamp", ulong(h.timestamp));
            g.done()
        }),
    );
    f.list(
        "entity",
        message
            .entity
            .iter()
            .map(|e| {
                let mut g = Fields::new();
                g.put("id", text(&e.id));
                g.put("trip_update", e.trip_update.as_ref().map(trip_update));
                g.put("vehicle", e.vehicle.as_ref().map(vehicle_position));
                g.put("alert", e.alert.as_ref().map(alert));
                g.done()
            })
            .collect(),
    );
    Ok(f.done())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_are_the_shortest_float32_text() {
        assert_eq!(shortest_float(0.1), 0.1);
        assert_eq!(shortest_float(50.081_234), 50.081_234);
        assert_eq!(shortest_float(90.0), 90.0);
    }
}
