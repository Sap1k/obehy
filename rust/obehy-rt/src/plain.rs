//! `canonical.plain` of the core's effects (`model.Effect`): each a dataclass object of its
//! fields plus `"@type"`, an observation only as its reference (`ObservationRef`), enums as
//! their value, times as UTC text. The compare harness's `effect` records hold these.

use obehy_core::model::{Effect, Interval, Observation, Platform, VehicleId};

use crate::canonical::{C, utc_text};
use crate::store::recording::{journey_plain, snapshot_plain};

pub fn observation_ref(o: &Observation) -> C {
    C::obj([
        ("@type", C::str("ObservationRef")),
        ("source", C::str(&o.source)),
        ("channel", C::str(&o.channel)),
        ("received_at", C::str(utc_text(o.received_at))),
        ("sha256", C::str(&o.raw.sha256)),
        ("item", C::Int(i128::from(o.raw.item))),
    ])
}

pub fn vehicle_plain(v: &VehicleId) -> C {
    C::obj([
        ("@type", C::str("VehicleId")),
        ("source", C::str(&v.source)),
        ("source_vehicle_id", C::str(&v.source_vehicle_id)),
    ])
}

pub fn interval_plain(i: &Interval) -> C {
    C::obj([
        ("@type", C::str("Interval")),
        ("lo", C::str(utc_text(i.lo))),
        ("hi", C::str(utc_text(i.hi))),
    ])
}

pub fn platform_plain(p: &Platform) -> C {
    C::obj([
        ("@type", C::str("Platform")),
        ("value", C::str(&p.value)),
        ("label", C::str(p.label.as_str())),
        (
            "boarding_point_id",
            C::opt_str(p.boarding_point_id.as_deref()),
        ),
        ("assigned_at", C::str(utc_text(p.assigned_at))),
        ("source", C::str(&p.source)),
    ])
}

pub fn effect_plain(effect: &Effect) -> C {
    match effect {
        Effect::SnapshotJourney(s) => snapshot_plain(s),
        Effect::WriteEvent(e) => C::obj([
            ("@type", C::str("WriteEvent")),
            ("journey", journey_plain(&e.journey)),
            ("location_id", C::str(&e.location_id)),
            ("visit_n", C::Int(i128::from(e.visit_n))),
            ("kind", C::str(e.kind.as_str())),
            ("interval", interval_plain(&e.interval)),
            ("method", C::str(e.method.as_str())),
            ("source", C::str(&e.source)),
        ]),
        Effect::AssignVehicle(a) => C::obj([
            ("@type", C::str("AssignVehicle")),
            ("vehicle", vehicle_plain(&a.vehicle)),
            ("journey", journey_plain(&a.journey)),
            ("at", C::str(utc_text(a.at))),
            ("method", C::str(a.method.as_str())),
        ]),
        Effect::ObservationResult(r) => C::obj([
            ("@type", C::str("ObservationResult")),
            ("observation", observation_ref(&r.observation)),
            ("journey", r.journey.as_ref().map_or(C::Null, journey_plain)),
            ("reason", C::opt_str(r.reason.map(|x| x.as_str()))),
            (
                "public",
                C::List(r.public.iter().map(journey_plain).collect()),
            ),
            ("state_since", C::time(r.state_since)),
            ("method", C::str(r.method.as_str())),
        ]),
        Effect::LinkJourneys(l) => C::obj([
            ("@type", C::str("LinkJourneys")),
            ("source", journey_plain(&l.source)),
            ("target", journey_plain(&l.target)),
            ("kind", C::str(l.kind.as_str())),
        ]),
        Effect::RecordPlatform(p) => C::obj([
            ("@type", C::str("RecordPlatform")),
            ("journey", journey_plain(&p.journey)),
            ("location_id", C::str(&p.location_id)),
            ("visit_n", C::Int(i128::from(p.visit_n))),
            ("kind", C::str(p.kind.as_str())),
            ("platform", platform_plain(&p.platform)),
        ]),
        Effect::RecordCancellation(c) => C::obj([
            ("@type", C::str("RecordCancellation")),
            ("journey", journey_plain(&c.journey)),
            ("source", C::str(&c.source)),
            ("at", C::str(utc_text(c.at))),
            ("cancelled", C::Bool(c.cancelled)),
        ]),
        Effect::Unresolved(u) => C::obj([
            ("@type", C::str("Unresolved")),
            ("observation", observation_ref(&u.observation)),
            ("kind", C::str(u.kind.as_str())),
            ("name", C::str(&u.name)),
        ]),
    }
}
