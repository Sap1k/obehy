//! A [`Store`] that records the writer's rows instead of sending them (`obehy-rt compare --level
//! output`).
//!
//! Every row the SQL store would be sent becomes a record in `pending`, in the order the writer
//! produced it; the harness takes them with `take` and adds `at`. The vehicle book starts as on
//! an empty database: no registry, no stored pairings or attributes. Partitions and terms are
//! storage details and are not recorded; transactions are not marked. The record kinds are the
//! Python module's (read its docstring).

use obehy_core::model::{Derivation, Feed, JourneyKey, SnapshotJourney};
use obehy_core::time::Date;

use super::cells::Row;
use super::vehicles::{Evidence, ProfileRow, RegistryRows, StoredVehicles, VehicleRows};
use super::{
    ASSIGNMENT_COLUMNS, CANCELLATION_COLUMNS, EVENT_COLUMNS, LINK_COLUMNS, OBSERVATION_COLUMNS,
    PLATFORM_COLUMNS, Prepare, StateTable, Store, StoreResult,
};
use crate::canonical::{obj, opt_text, text, time, utc_text};
use serde_json::Value;

/// A row as an object of its columns.
pub fn named(columns: &[&str], row: &Row) -> Value {
    assert_eq!(columns.len(), row.len(), "a row has one cell per column");
    Value::Object(
        columns
            .iter()
            .zip(row)
            .map(|(name, cell)| ((*name).to_owned(), cell.record()))
            .collect(),
    )
}

/// `canonical.plain` of a `JourneyKey`.
pub fn journey_plain(journey: &JourneyKey) -> Value {
    obj([
        ("@type", text("JourneyKey")),
        ("feed", text(journey.feed.as_str())),
        ("namespace", text(&journey.namespace)),
        ("key", text(&journey.key)),
        ("service_date", text(journey.service_date.isoformat())),
    ])
}

fn opt_int(value: Option<i32>) -> Value {
    value.map_or(Value::Null, Value::from)
}

/// `canonical.plain` of a `SnapshotJourney`.
pub fn snapshot_plain(s: &SnapshotJourney) -> Value {
    let calls = s
        .calls
        .iter()
        .map(|c| {
            obj([
                ("@type", text("ScheduledCall")),
                ("ordinal", Value::from(c.ordinal)),
                ("location_id", text(&c.location_id)),
                ("visit_n", Value::from(c.visit_n)),
                ("passenger_service", Value::Bool(c.passenger_service)),
                ("scheduled_arrival", opt_int(c.scheduled_arrival)),
                ("scheduled_departure", opt_int(c.scheduled_departure)),
                ("name", text(&c.name)),
            ])
        })
        .collect();
    obj([
        ("@type", text("SnapshotJourney")),
        ("journey", journey_plain(&s.journey)),
        ("at", text(utc_text(s.at))),
        ("release_id", text(&s.release_id)),
        ("trip_id", text(&s.trip_id)),
        ("route_name", text(&s.route_name)),
        ("headsign", opt_text(s.headsign.as_deref())),
        ("calls", Value::Array(calls)),
        ("run_key", opt_text(s.run_key.as_deref())),
        ("route_id", opt_text(s.route_id.as_deref())),
        ("mode", opt_text(s.mode.as_deref())),
        ("agency_id", opt_text(s.agency_id.as_deref())),
    ])
}

/// `canonical.plain` of an `Evidence`.
pub fn evidence_plain(e: &Evidence) -> Value {
    obj([
        ("@type", text("Evidence")),
        ("first_seen", text(utc_text(e.first_seen))),
        ("last_seen", text(utc_text(e.last_seen))),
        ("fixes", Value::from(e.fixes)),
        ("journey", journey_plain(&e.journey)),
        ("confirmed", Value::Bool(e.confirmed)),
    ])
}

/// `canonical.plain` of a `Profile`.
pub fn profile_plain(p: &ProfileRow) -> Value {
    obj([
        ("@type", text("Profile")),
        ("vehicle_id", text(&p.vehicle_id)),
        ("registered", Value::Bool(p.registered)),
        ("public_label", opt_text(p.public_label.as_deref())),
        (
            "flags",
            Value::Object(
                p.flags
                    .iter()
                    .map(|(k, v)| (k.clone(), v.map_or(Value::Null, Value::Bool)))
                    .collect(),
            ),
        ),
        (
            "texts",
            Value::Object(
                p.texts
                    .iter()
                    .map(|(k, v)| (k.clone(), opt_text(v.as_deref())))
                    .collect(),
            ),
        ),
        (
            "datasets",
            Value::Array(p.datasets.iter().map(text).collect()),
        ),
        (
            "sources",
            Value::Array(p.sources.iter().map(text).collect()),
        ),
        ("attributes", Value::Array(p.attributes.to_vec())),
        ("last_stated", time(p.last_stated)),
    ])
}

fn report(r: &(String, String)) -> Value {
    Value::Array(vec![text(&r.0), text(&r.1)])
}

/// The rows the SQL store would be sent, as records.
#[derive(Debug, Default)]
pub struct RecordingStore {
    pub pending: Vec<Value>,
    /// `rt.physical_vehicles`' rows, recorded when the writer opens.
    pub physical: Vec<(String, String, bool)>,
}

impl RecordingStore {
    pub fn new(physical: Vec<(String, String, bool)>) -> RecordingStore {
        RecordingStore {
            pending: Vec::new(),
            physical,
        }
    }

    /// The records made since the last call.
    pub fn take(&mut self) -> Vec<Value> {
        std::mem::take(&mut self.pending)
    }

    fn rows(&mut self, kind: &str, columns: &[&str], rows: &[Row]) {
        self.pending.extend(
            rows.iter()
                .map(|row| obj([("kind", text(kind)), ("row", named(columns, row))])),
        );
    }
}

impl Store for RecordingStore {
    fn open(&mut self, _: &Derivation) -> StoreResult<StoredVehicles> {
        let rows: Vec<Value> = self
            .physical
            .iter()
            .map(|(source, mode, physical)| {
                obj([
                    ("kind", text("physical_vehicles")),
                    (
                        "row",
                        obj([
                            ("source", text(source)),
                            ("mode", text(mode)),
                            ("physical", Value::Bool(*physical)),
                        ]),
                    ),
                ])
            })
            .collect();
        self.pending.extend(rows);
        Ok(StoredVehicles::default())
    }

    fn reload_registry(&mut self) -> StoreResult<RegistryRows> {
        Ok(RegistryRows::default())
    }

    fn prepare(&mut self, _: &Prepare) -> StoreResult<()> {
        Ok(())
    }

    fn begin(&mut self) -> StoreResult<()> {
        Ok(())
    }

    fn commit(&mut self) -> StoreResult<()> {
        Ok(())
    }

    fn rollback(&mut self) {}

    fn observations(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.rows("observation", &OBSERVATION_COLUMNS, rows);
        Ok(())
    }

    fn snapshots(&mut self, snapshots: &[&SnapshotJourney]) -> StoreResult<()> {
        for snapshot in snapshots {
            self.pending.push(obj([
                ("kind", text("journey_schedule")),
                ("snapshot", snapshot_plain(snapshot)),
            ]));
        }
        Ok(())
    }

    fn events(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.rows("stop_event", &EVENT_COLUMNS, rows);
        Ok(())
    }

    fn assignments(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.rows("vehicle_assignment", &ASSIGNMENT_COLUMNS, rows);
        Ok(())
    }

    fn links(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.rows("journey_link", &LINK_COLUMNS, rows);
        Ok(())
    }

    fn platforms(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.rows("platform_evidence", &PLATFORM_COLUMNS, rows);
        Ok(())
    }

    fn cancellations(&mut self, rows: &[Row]) -> StoreResult<()> {
        self.rows("journey_cancellation", &CANCELLATION_COLUMNS, rows);
        Ok(())
    }

    fn vehicles(&mut self, rows: &VehicleRows) -> StoreResult<()> {
        for a in &rows.attributes {
            self.pending.push(obj([
                ("kind", text("vehicle_attribute")),
                (
                    "row",
                    obj([
                        ("source", text(&a.source)),
                        ("source_vehicle_id", text(&a.source_vehicle_id)),
                        ("attribute", text(&a.attribute)),
                        ("value", text(&a.value)),
                        ("observed_at", text(utc_text(a.observed_at))),
                    ]),
                ),
            ]));
        }
        for (a, b) in &rows.gone {
            self.pending.push(obj([
                ("kind", text("vehicle_pairing")),
                ("op", text("delete")),
                ("a", report(a)),
                ("b", report(b)),
            ]));
        }
        for ((a, b), evidence) in &rows.kept {
            self.pending.push(obj([
                ("kind", text("vehicle_pairing")),
                ("op", text("upsert")),
                ("a", report(a)),
                ("b", report(b)),
                ("evidence", evidence_plain(evidence)),
            ]));
        }
        if rows.rewrite {
            self.pending
                .push(obj([("kind", text("vehicle_profile_reset"))]));
        }
        for p in &rows.profiles {
            self.pending.push(obj([
                ("kind", text("vehicle_profile")),
                ("profile", profile_plain(p)),
            ]));
        }
        Ok(())
    }

    fn replace_state(&mut self, table: StateTable, feed: Feed, rows: &[Row]) -> StoreResult<()> {
        self.pending.push(obj([
            ("kind", text("state")),
            ("table", text(table.name)),
            ("op", text("replace")),
            ("feed", text(feed.as_str())),
        ]));
        for row in rows {
            self.pending.push(obj([
                ("kind", text("state")),
                ("table", text(table.name)),
                ("op", text("insert")),
                ("row", named(table.columns, row)),
            ]));
        }
        Ok(())
    }

    fn delete_state(&mut self, table: StateTable, keys: &[Row]) -> StoreResult<()> {
        for key in keys {
            self.pending.push(obj([
                ("kind", text("state")),
                ("table", text(table.name)),
                ("op", text("delete")),
                ("key", named(table.key, key)),
            ]));
        }
        Ok(())
    }

    fn upsert_state(&mut self, table: StateTable, rows: &[Row]) -> StoreResult<()> {
        for row in rows {
            self.pending.push(obj([
                ("kind", text("state")),
                ("table", text(table.name)),
                ("op", text("upsert")),
                ("row", named(table.columns, row)),
            ]));
        }
        Ok(())
    }

    fn clear_history(&mut self, days: &[Date]) -> StoreResult<()> {
        self.pending.push(obj([
            ("kind", text("clear_history")),
            (
                "days",
                Value::Array(days.iter().map(|d| text(d.isoformat())).collect()),
            ),
        ]));
        Ok(())
    }
}
