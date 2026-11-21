//! `canonical.plain` of the core's effects (`model.Effect`): each a dataclass object of its
//! fields plus `"@type"`, an observation only as its reference (`ObservationRef`), enums as
//! their value, times as UTC text. The compare harness's `effect` records hold these.

use obehy_core::model::{Effect, Interval, Observation, Platform, VehicleId};

use crate::canonical::{obj, opt_text, text, time, utc_text};
use crate::store::recording::{journey_plain, snapshot_plain};
use serde_json::Value;

pub fn observation_ref(o: &Observation) -> Value {
    obj([
        ("@type", text("ObservationRef")),
        ("source", text(&o.source)),
        ("channel", text(&o.channel)),
        ("received_at", text(utc_text(o.received_at))),
        ("sha256", text(&o.raw.sha256)),
        ("item", Value::from(o.raw.item)),
    ])
}

pub fn vehicle_plain(v: &VehicleId) -> Value {
    obj([
        ("@type", text("VehicleId")),
        ("source", text(&v.source)),
        ("source_vehicle_id", text(&v.source_vehicle_id)),
    ])
}

pub fn interval_plain(i: &Interval) -> Value {
    obj([
        ("@type", text("Interval")),
        ("lo", text(utc_text(i.lo))),
        ("hi", text(utc_text(i.hi))),
    ])
}

pub fn platform_plain(p: &Platform) -> Value {
    obj([
        ("@type", text("Platform")),
        ("value", text(&p.value)),
        ("label", text(p.label.as_str())),
        (
            "boarding_point_id",
            opt_text(p.boarding_point_id.as_deref()),
        ),
        ("assigned_at", text(utc_text(p.assigned_at))),
        ("source", text(&p.source)),
    ])
}

pub fn effect_plain(effect: &Effect) -> Value {
    match effect {
        Effect::SnapshotJourney(s) => snapshot_plain(s),
        Effect::WriteEvent(e) => obj([
            ("@type", text("WriteEvent")),
            ("journey", journey_plain(&e.journey)),
            ("location_id", text(&e.location_id)),
            ("visit_n", Value::from(e.visit_n)),
            ("kind", text(e.kind.as_str())),
            ("interval", interval_plain(&e.interval)),
            ("method", text(e.method.as_str())),
            ("source", text(&e.source)),
        ]),
        Effect::AssignVehicle(a) => obj([
            ("@type", text("AssignVehicle")),
            ("vehicle", vehicle_plain(&a.vehicle)),
            ("journey", journey_plain(&a.journey)),
            ("at", text(utc_text(a.at))),
            ("method", text(a.method.as_str())),
        ]),
        Effect::ObservationResult(r) => obj([
            ("@type", text("ObservationResult")),
            ("observation", observation_ref(&r.observation)),
            (
                "journey",
                r.journey.as_ref().map_or(Value::Null, journey_plain),
            ),
            ("reason", opt_text(r.reason.map(|x| x.as_str()))),
            (
                "public",
                Value::Array(r.public.iter().map(journey_plain).collect()),
            ),
            ("state_since", time(r.state_since)),
            ("method", text(r.method.as_str())),
        ]),
        Effect::LinkJourneys(l) => obj([
            ("@type", text("LinkJourneys")),
            ("source", journey_plain(&l.source)),
            ("target", journey_plain(&l.target)),
            ("kind", text(l.kind.as_str())),
        ]),
        Effect::RecordPlatform(p) => obj([
            ("@type", text("RecordPlatform")),
            ("journey", journey_plain(&p.journey)),
            ("location_id", text(&p.location_id)),
            ("visit_n", Value::from(p.visit_n)),
            ("kind", text(p.kind.as_str())),
            ("platform", platform_plain(&p.platform)),
        ]),
        Effect::RecordCancellation(c) => obj([
            ("@type", text("RecordCancellation")),
            ("journey", journey_plain(&c.journey)),
            ("source", text(&c.source)),
            ("at", text(utc_text(c.at))),
            ("cancelled", Value::Bool(c.cancelled)),
        ]),
        Effect::Unresolved(u) => obj([
            ("@type", text("Unresolved")),
            ("observation", observation_ref(&u.observation)),
            ("kind", text(u.kind.as_str())),
            ("name", text(&u.name)),
        ]),
    }
}
