//! A [`Store`] that records the writer's rows instead of sending them: port of
//! `emit/recording.py` (`obehy rt compare --level output`).
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
use crate::canonical::{C, utc_text};

/// A row as an object of its columns.
pub fn named(columns: &[&str], row: &Row) -> C {
    assert_eq!(columns.len(), row.len(), "a row has one cell per column");
    C::Obj(
        columns
            .iter()
            .zip(row)
            .map(|(name, cell)| ((*name).to_owned(), cell.record()))
            .collect(),
    )
}

/// `canonical.plain` of a `JourneyKey`.
pub fn journey_plain(journey: &JourneyKey) -> C {
    C::obj([
        ("@type", C::str("JourneyKey")),
        ("feed", C::str(journey.feed.as_str())),
        ("namespace", C::str(&journey.namespace)),
        ("key", C::str(&journey.key)),
        ("service_date", C::str(journey.service_date.isoformat())),
    ])
}

fn opt_int(value: Option<i32>) -> C {
    value.map_or(C::Null, |v| C::Int(i128::from(v)))
}

/// `canonical.plain` of a `SnapshotJourney`.
pub fn snapshot_plain(s: &SnapshotJourney) -> C {
    let calls = s
        .calls
        .iter()
        .map(|c| {
            C::obj([
                ("@type", C::str("ScheduledCall")),
                ("ordinal", C::Int(i128::from(c.ordinal))),
                ("location_id", C::str(&c.location_id)),
                ("visit_n", C::Int(i128::from(c.visit_n))),
                ("passenger_service", C::Bool(c.passenger_service)),
                ("scheduled_arrival", opt_int(c.scheduled_arrival)),
                ("scheduled_departure", opt_int(c.scheduled_departure)),
                ("name", C::str(&c.name)),
            ])
        })
        .collect();
    C::obj([
        ("@type", C::str("SnapshotJourney")),
        ("journey", journey_plain(&s.journey)),
        ("at", C::str(utc_text(s.at))),
        ("release_id", C::str(&s.release_id)),
        ("trip_id", C::str(&s.trip_id)),
        ("route_name", C::str(&s.route_name)),
        ("headsign", C::opt_str(s.headsign.as_deref())),
        ("calls", C::List(calls)),
        ("run_key", C::opt_str(s.run_key.as_deref())),
        ("route_id", C::opt_str(s.route_id.as_deref())),
        ("mode", C::opt_str(s.mode.as_deref())),
        ("agency_id", C::opt_str(s.agency_id.as_deref())),
    ])
}

/// `canonical.plain` of an `Evidence`.
pub fn evidence_plain(e: &Evidence) -> C {
    C::obj([
        ("@type", C::str("Evidence")),
        ("first_seen", C::str(utc_text(e.first_seen))),
        ("last_seen", C::str(utc_text(e.last_seen))),
        ("fixes", C::Int(i128::from(e.fixes))),
        ("journey", journey_plain(&e.journey)),
        ("confirmed", C::Bool(e.confirmed)),
    ])
}

/// `canonical.plain` of a `Profile`.
pub fn profile_plain(p: &ProfileRow) -> C {
    C::obj([
        ("@type", C::str("Profile")),
        ("vehicle_id", C::str(&p.vehicle_id)),
        ("registered", C::Bool(p.registered)),
        ("public_label", C::opt_str(p.public_label.as_deref())),
        (
            "flags",
            C::Obj(
                p.flags
                    .iter()
                    .map(|(k, v)| (k.clone(), v.map_or(C::Null, C::Bool)))
                    .collect(),
            ),
        ),
        (
            "texts",
            C::Obj(
                p.texts
                    .iter()
                    .map(|(k, v)| (k.clone(), C::opt_str(v.as_deref())))
                    .collect(),
            ),
        ),
        ("datasets", C::List(p.datasets.iter().map(C::str).collect())),
        ("sources", C::List(p.sources.iter().map(C::str).collect())),
        (
            "attributes",
            C::List(p.attributes.iter().map(C::from_py).collect()),
        ),
        ("last_stated", C::time(p.last_stated)),
    ])
}

fn report(r: &(String, String)) -> C {
    C::List(vec![C::str(&r.0), C::str(&r.1)])
}

/// The rows the SQL store would be sent, as records.
#[derive(Debug, Default)]
pub struct RecordingStore {
    pub pending: Vec<C>,
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
    pub fn take(&mut self) -> Vec<C> {
        std::mem::take(&mut self.pending)
    }

    fn rows(&mut self, kind: &str, columns: &[&str], rows: &[Row]) {
        self.pending.extend(
            rows.iter()
                .map(|row| C::obj([("kind", C::str(kind)), ("row", named(columns, row))])),
        );
    }
}

impl Store for RecordingStore {
    fn open(&mut self, _: &Derivation) -> StoreResult<StoredVehicles> {
        let rows: Vec<C> = self
            .physical
            .iter()
            .map(|(source, mode, physical)| {
                C::obj([
                    ("kind", C::str("physical_vehicles")),
                    (
                        "row",
                        C::obj([
                            ("source", C::str(source)),
                            ("mode", C::str(mode)),
                            ("physical", C::Bool(*physical)),
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
            self.pending.push(C::obj([
                ("kind", C::str("journey_schedule")),
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
            self.pending.push(C::obj([
                ("kind", C::str("vehicle_attribute")),
                (
                    "row",
                    C::obj([
                        ("source", C::str(&a.source)),
                        ("source_vehicle_id", C::str(&a.source_vehicle_id)),
                        ("attribute", C::str(&a.attribute)),
                        ("value", C::str(&a.value)),
                        ("observed_at", C::str(utc_text(a.observed_at))),
                    ]),
                ),
            ]));
        }
        for (a, b) in &rows.gone {
            self.pending.push(C::obj([
                ("kind", C::str("vehicle_pairing")),
                ("op", C::str("delete")),
                ("a", report(a)),
                ("b", report(b)),
            ]));
        }
        for ((a, b), evidence) in &rows.kept {
            self.pending.push(C::obj([
                ("kind", C::str("vehicle_pairing")),
                ("op", C::str("upsert")),
                ("a", report(a)),
                ("b", report(b)),
                ("evidence", evidence_plain(evidence)),
            ]));
        }
        if rows.rewrite {
            self.pending
                .push(C::obj([("kind", C::str("vehicle_profile_reset"))]));
        }
        for p in &rows.profiles {
            self.pending.push(C::obj([
                ("kind", C::str("vehicle_profile")),
                ("profile", profile_plain(p)),
            ]));
        }
        Ok(())
    }

    fn replace_state(&mut self, table: StateTable, feed: Feed, rows: &[Row]) -> StoreResult<()> {
        self.pending.push(C::obj([
            ("kind", C::str("state")),
            ("table", C::str(table.name)),
            ("op", C::str("replace")),
            ("feed", C::str(feed.as_str())),
        ]));
        for row in rows {
            self.pending.push(C::obj([
                ("kind", C::str("state")),
                ("table", C::str(table.name)),
                ("op", C::str("insert")),
                ("row", named(table.columns, row)),
            ]));
        }
        Ok(())
    }

    fn delete_state(&mut self, table: StateTable, keys: &[Row]) -> StoreResult<()> {
        for key in keys {
            self.pending.push(C::obj([
                ("kind", C::str("state")),
                ("table", C::str(table.name)),
                ("op", C::str("delete")),
                ("key", named(table.key, key)),
            ]));
        }
        Ok(())
    }

    fn upsert_state(&mut self, table: StateTable, rows: &[Row]) -> StoreResult<()> {
        for row in rows {
            self.pending.push(C::obj([
                ("kind", C::str("state")),
                ("table", C::str(table.name)),
                ("op", C::str("upsert")),
                ("row", named(table.columns, row)),
            ]));
        }
        Ok(())
    }

    fn clear_history(&mut self, days: &[Date]) -> StoreResult<()> {
        self.pending.push(C::obj([
            ("kind", C::str("clear_history")),
            (
                "days",
                C::List(days.iter().map(|d| C::str(d.isoformat())).collect()),
            ),
        ]));
        Ok(())
    }
}
