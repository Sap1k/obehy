//! An output stream's row records sent to a store again: what [`super::RecordingStore`] (and
//! Python's `emit/recording.py`) recorded, as the calls the writer made. The store parity check
//! (`examples/store_parity.rs`, `rust/tools/store_db.py parity`) sends one stream through
//! Python's `SqlStore` and through [`super::PgStore`] and compares the tables.
//!
//! Records are grouped by write: the rows after an effect, header or GTFS-RT record, split again
//! where state rows start (a snapshot's `write_state` follows a tick's `write` with nothing
//! between them); `physical_vehicles` is the store's opening and is skipped.

use std::collections::BTreeSet;
use std::io::BufRead;
use std::sync::Arc;

use obehy_core::model::json::facts_from_json;
use obehy_core::model::{
    AssignVehicle, BindingMethod, Effect, EventKind, EventMethod, Feed, Interval, JourneyKey,
    JourneyLinkKind, LinkJourneys, Observation, ObservationResult, Platform, PlatformLabel, RawRef,
    Reason, RecordCancellation, RecordPlatform, ScheduledCall, SnapshotJourney, StopKind,
    VehicleId, WriteEvent,
};
use obehy_core::py::json::PyJson;
use obehy_core::time::{Date, Instant};
use serde_json::Value;

use super::cells::{Cell, Row};
use super::storage::hour_of;
use super::vehicles::{AttributeRow, Evidence, ProfileRow, Report, VehicleRows};
use super::{
    ASSIGNMENT_COLUMNS, CANCELLATION_COLUMNS, EVENT_COLUMNS, LINK_COLUMNS, OBSERVATION_COLUMNS,
    PLATFORM_COLUMNS, Prepare, STATE_TABLES, StateTable, Store, StoreError, StoreResult,
};

const TIMES: [&str; 11] = [
    "received_at",
    "observed_at",
    "state_since",
    "event_time",
    "interval_lo",
    "interval_hi",
    "first_seen",
    "last_seen",
    "at",
    "updated_at",
    "position_at",
];
const FLOATS: [&str; 3] = ["latitude", "longitude", "bearing"];
const JSON: [&str; 3] = ["facts", "calls", "sources"];

fn bad(message: impl Into<String>) -> StoreError {
    StoreError::other(message)
}

/// A JSON value as Python's `json.loads` gives it.
pub fn py_of(value: &Value) -> PyJson {
    match value {
        Value::Null => PyJson::Null,
        Value::Bool(b) => PyJson::Bool(*b),
        Value::Number(n) => n.as_i64().map_or_else(
            || PyJson::Float(n.as_f64().unwrap_or(f64::NAN)),
            PyJson::Int,
        ),
        Value::String(s) => PyJson::Str(s.clone()),
        Value::Array(items) => PyJson::List(items.iter().map(py_of).collect()),
        Value::Object(map) => {
            PyJson::Object(map.iter().map(|(k, v)| (k.clone(), py_of(v))).collect())
        }
    }
}

fn instant(value: &Value) -> StoreResult<Instant> {
    let text = value
        .as_str()
        .ok_or_else(|| bad(format!("{value} is no time")))?;
    let text = text
        .strip_suffix('Z')
        .map_or_else(|| text.to_owned(), |t| format!("{t}+00:00"));
    Instant::from_isoformat(&text).map_err(|e| bad(e.0))
}

fn date(value: &Value) -> StoreResult<Date> {
    Date::from_isoformat(value.as_str().unwrap_or_default()).map_err(|e| bad(e.0))
}

fn text(value: &Value) -> String {
    value.as_str().unwrap_or_default().to_owned()
}

fn opt_text(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}

/// One recorded cell of `column`.
pub fn cell(column: &str, value: &Value) -> StoreResult<Cell> {
    if value.is_null() {
        return Ok(Cell::Null);
    }
    if TIMES.contains(&column) {
        return instant(value).map(Cell::Time);
    }
    if column == "service_date" {
        return date(value).map(Cell::Date);
    }
    if JSON.contains(&column) {
        return Ok(Cell::json(py_of(value)));
    }
    if FLOATS.contains(&column) {
        return value
            .as_f64()
            .map(Cell::Float)
            .ok_or_else(|| bad(format!("{column}: {value} is no number")));
    }
    Ok(match value {
        Value::Bool(b) => Cell::Bool(*b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => Cell::Int(i),
            None => Cell::Float(n.as_f64().unwrap_or(f64::NAN)),
        },
        Value::String(s) => Cell::Text(s.clone()),
        other => return Err(bad(format!("{column}: unexpected {other}"))),
    })
}

/// A recorded row object as a row of `columns`.
pub fn row(columns: &[&str], object: &Value) -> StoreResult<Row> {
    columns
        .iter()
        .map(|c| cell(c, object.get(*c).unwrap_or(&Value::Null)))
        .collect()
}

fn journey(value: &Value) -> StoreResult<JourneyKey> {
    Ok(JourneyKey {
        feed: text(&value["feed"]).parse::<Feed>().map_err(bad)?,
        namespace: text(&value["namespace"]),
        key: text(&value["key"]),
        service_date: date(&value["service_date"])?,
    })
}

fn int(value: &Value) -> i64 {
    value.as_i64().unwrap_or_default()
}

/// A recorded `SnapshotJourney`.
pub fn snapshot(value: &Value) -> StoreResult<SnapshotJourney> {
    let calls: Vec<ScheduledCall> = value["calls"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(|c| ScheduledCall {
            ordinal: int(&c["ordinal"]),
            location_id: text(&c["location_id"]),
            visit_n: u32::try_from(int(&c["visit_n"])).unwrap_or_default(),
            passenger_service: c["passenger_service"].as_bool().unwrap_or_default(),
            scheduled_arrival: c["scheduled_arrival"]
                .as_i64()
                .and_then(|v| i32::try_from(v).ok()),
            scheduled_departure: c["scheduled_departure"]
                .as_i64()
                .and_then(|v| i32::try_from(v).ok()),
            name: text(&c["name"]),
        })
        .collect();
    Ok(SnapshotJourney {
        journey: journey(&value["journey"])?,
        at: instant(&value["at"])?,
        release_id: text(&value["release_id"]),
        trip_id: text(&value["trip_id"]),
        route_name: text(&value["route_name"]),
        headsign: opt_text(&value["headsign"]),
        calls: Arc::from(calls),
        run_key: opt_text(&value["run_key"]),
        route_id: opt_text(&value["route_id"]),
        mode: opt_text(&value["mode"]),
        agency_id: opt_text(&value["agency_id"]),
    })
}

fn report(value: &Value) -> Report {
    (text(&value[0]), text(&value[1]))
}

fn profile(value: &Value) -> StoreResult<ProfileRow> {
    let pairs = |v: &Value| -> Vec<(String, Value)> {
        v.as_object()
            .map(|o| o.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default()
    };
    let strings = |v: &Value| -> Vec<String> {
        v.as_array()
            .map(|a| a.iter().map(text).collect())
            .unwrap_or_default()
    };
    Ok(ProfileRow {
        vehicle_id: text(&value["vehicle_id"]),
        registered: value["registered"].as_bool().unwrap_or_default(),
        public_label: opt_text(&value["public_label"]),
        flags: pairs(&value["flags"])
            .into_iter()
            .map(|(k, v)| (k, v.as_bool()))
            .collect(),
        texts: pairs(&value["texts"])
            .into_iter()
            .map(|(k, v)| (k, opt_text(&v)))
            .collect(),
        datasets: strings(&value["datasets"]),
        sources: strings(&value["sources"]),
        attributes: value["attributes"]
            .as_array()
            .map(|a| a.iter().map(py_of).collect())
            .unwrap_or_default(),
        last_stated: match &value["last_stated"] {
            Value::Null => None,
            v => Some(instant(v)?),
        },
    })
}

/// The book's rows among `records`.
pub fn vehicle_rows(records: &[Value]) -> StoreResult<VehicleRows> {
    let mut rows = VehicleRows::default();
    for r in records {
        match r["kind"].as_str() {
            Some("vehicle_attribute") => {
                let row = &r["row"];
                rows.attributes.push(AttributeRow {
                    source: text(&row["source"]),
                    source_vehicle_id: text(&row["source_vehicle_id"]),
                    attribute: text(&row["attribute"]),
                    value: text(&row["value"]),
                    observed_at: instant(&row["observed_at"])?,
                });
            }
            Some("vehicle_pairing") if r["op"] == "delete" => {
                rows.gone.push((report(&r["a"]), report(&r["b"])));
            }
            Some("vehicle_pairing") => {
                let e = &r["evidence"];
                rows.kept.push((
                    (report(&r["a"]), report(&r["b"])),
                    Evidence {
                        first_seen: instant(&e["first_seen"])?,
                        last_seen: instant(&e["last_seen"])?,
                        fixes: int(&e["fixes"]),
                        journey: journey(&e["journey"])?,
                        confirmed: e["confirmed"].as_bool().unwrap_or_default(),
                    },
                ));
            }
            Some("vehicle_profile_reset") => rows.rewrite = true,
            Some("vehicle_profile") => rows.profiles.push(profile(&r["profile"])?),
            _ => {}
        }
    }
    Ok(rows)
}

const BOUNDARIES: [&str; 5] = ["header", "effect", "gtfs_rt", "poll", "predictor_sample"];

/// The stream's row records grouped by write, read as they are needed.
pub fn groups<R: BufRead>(lines: R, limit: Option<usize>) -> Groups<R> {
    Groups {
        lines,
        limit,
        count: 0,
        pending: None,
        done: false,
    }
}

/// The iterator of [`groups`].
#[derive(Debug)]
pub struct Groups<R> {
    lines: R,
    limit: Option<usize>,
    count: usize,
    /// A state record that starts the next group.
    pending: Option<Value>,
    done: bool,
}

impl<R: BufRead> Iterator for Groups<R> {
    type Item = StoreResult<Vec<Value>>;

    fn next(&mut self) -> Option<Self::Item> {
        let mut group: Vec<Value> = self.pending.take().into_iter().collect();
        let mut line = String::new();
        while !self.done {
            line.clear();
            match self.lines.read_line(&mut line) {
                Ok(0) => {
                    self.done = true;
                    break;
                }
                Ok(_) => {}
                Err(e) => return Some(Err(bad(e.to_string()))),
            }
            let record: Value = match serde_json::from_str(line.trim_end()) {
                Ok(record) => record,
                Err(e) => return Some(Err(bad(e.to_string()))),
            };
            let kind = record["kind"].as_str().unwrap_or_default();
            if BOUNDARIES.contains(&kind) {
                if group.is_empty() {
                    continue;
                }
                return Some(Ok(group));
            }
            self.count += 1;
            if self.limit.is_some_and(|l| self.count >= l) {
                self.done = true;
            }
            if kind == "state" && !group.is_empty() && !group.iter().any(|r| r["kind"] == "state") {
                self.pending = Some(record);
                return Some(Ok(group));
            }
            group.push(record);
        }
        (!group.is_empty()).then_some(Ok(group))
    }
}

fn of(group: &[Value], kind: &str, columns: &[&str]) -> StoreResult<Vec<Row>> {
    group
        .iter()
        .filter(|r| r["kind"] == kind)
        .map(|r| row(columns, &r["row"]))
        .collect()
}

fn table(name: &str) -> StoreResult<StateTable> {
    STATE_TABLES
        .into_iter()
        .find(|t| t.name == name)
        .ok_or_else(|| bad(format!("unknown state table {name}")))
}

/// A `write_state`'s calls again: `replace` with its inserts, runs of deletes and of upserts.
fn states<S: Store>(store: &mut S, records: &[&Value]) -> StoreResult<()> {
    let mut i = 0;
    while i < records.len() {
        let record = records[i];
        let t = table(record["table"].as_str().unwrap_or_default())?;
        let op = record["op"].as_str().unwrap_or_default();
        let mut j = i + 1;
        if op == "replace" {
            while j < records.len() && records[j]["op"] == "insert" {
                j += 1;
            }
            let rows: Vec<Row> = records[i + 1..j]
                .iter()
                .map(|r| row(t.columns, &r["row"]))
                .collect::<StoreResult<_>>()?;
            let feed = text(&record["feed"]).parse::<Feed>().map_err(bad)?;
            store.replace_state(t, feed, &rows)?;
        } else {
            while j < records.len()
                && records[j]["op"] == op
                && records[j]["table"] == record["table"]
            {
                j += 1;
            }
            if op == "delete" {
                let keys: Vec<Row> = records[i..j]
                    .iter()
                    .map(|r| row(t.key, &r["key"]))
                    .collect::<StoreResult<_>>()?;
                store.delete_state(t, &keys)?;
            } else {
                let rows: Vec<Row> = records[i..j]
                    .iter()
                    .map(|r| row(t.columns, &r["row"]))
                    .collect::<StoreResult<_>>()?;
                store.upsert_state(t, &rows)?;
            }
        }
        i = j;
    }
    Ok(())
}

fn in_transaction<S: Store>(
    store: &mut S,
    body: impl FnOnce(&mut S) -> StoreResult<()>,
) -> StoreResult<()> {
    store.begin()?;
    match body(store) {
        Ok(()) => store.commit(),
        Err(error) => {
            store.rollback();
            Err(error)
        }
    }
}

/// One group of records sent to `store` as the writer sent it.
pub fn apply<S: Store>(store: &mut S, group: &[Value]) -> StoreResult<()> {
    let Some(first) = group.first() else {
        return Ok(());
    };
    if first["kind"] == "physical_vehicles" {
        return Ok(()); // written by open
    }
    if first["kind"] == "state" {
        let state: Vec<&Value> = group.iter().filter(|r| r["kind"] == "state").collect();
        let rest: Vec<Value> = group
            .iter()
            .filter(|r| r["kind"] != "state")
            .cloned()
            .collect();
        let vehicles = vehicle_rows(&rest)?;
        return in_transaction(store, |s| {
            states(s, &state)?;
            s.vehicles(&vehicles)
        });
    }
    let mut prepare = Prepare::default();
    let mut days = BTreeSet::new();
    for r in group {
        match r["kind"].as_str() {
            Some("observation") => {
                prepare
                    .hours
                    .insert(hour_of(instant(&r["row"]["received_at"])?));
            }
            Some("journey_schedule") => {
                days.insert(date(&r["snapshot"]["journey"]["service_date"])?);
                for c in r["snapshot"]["calls"]
                    .as_array()
                    .map(Vec::as_slice)
                    .unwrap_or_default()
                {
                    prepare.terms.push(text(&c["location_id"]));
                    prepare.terms.push(text(&c["name"]));
                }
            }
            Some("platform_evidence" | "journey_cancellation") => {
                days.insert(date(&r["row"]["service_date"])?);
            }
            Some("stop_event") => {
                for key in ["location", "method", "source"] {
                    prepare.terms.push(text(&r["row"][key]));
                }
            }
            _ => {}
        }
    }
    prepare.days = days;
    store.prepare(&prepare)?;
    let observations = of(group, "observation", &OBSERVATION_COLUMNS)?;
    let snapshots: Vec<SnapshotJourney> = group
        .iter()
        .filter(|r| r["kind"] == "journey_schedule")
        .map(|r| snapshot(&r["snapshot"]))
        .collect::<StoreResult<_>>()?;
    let events = of(group, "stop_event", &EVENT_COLUMNS)?;
    let assignments = of(group, "vehicle_assignment", &ASSIGNMENT_COLUMNS)?;
    let links = of(group, "journey_link", &LINK_COLUMNS)?;
    let platforms = of(group, "platform_evidence", &PLATFORM_COLUMNS)?;
    let cancellations = of(group, "journey_cancellation", &CANCELLATION_COLUMNS)?;
    let vehicles = vehicle_rows(group)?;
    in_transaction(store, |s| {
        s.observations(&observations)?;
        let snapshots: Vec<&SnapshotJourney> = snapshots.iter().collect();
        s.snapshots(&snapshots)?;
        s.events(&events)?;
        s.assignments(&assignments)?;
        s.links(&links)?;
        s.platforms(&platforms)?;
        s.cancellations(&cancellations)?;
        s.vehicles(&vehicles)
    })
}

/// The `header` record of a stream (its first line).
pub fn header(lines: impl BufRead) -> StoreResult<Value> {
    let first = lines
        .lines()
        .next()
        .ok_or_else(|| bad("an empty stream"))?
        .map_err(|e| bad(e.to_string()))?;
    serde_json::from_str(&first).map_err(|e| bad(e.to_string()))
}

// --- effects, for driving the writer from a stream ------------------------------------------

/// An observation as its `observation` row record states it (the facts are the row's).
pub fn observation_of(row: &Value) -> StoreResult<Observation> {
    Ok(Observation {
        source: text(&row["source"]),
        channel: text(&row["channel"]),
        feed: text(&row["feed"]).parse::<Feed>().map_err(bad)?,
        received_at: instant(&row["received_at"])?,
        observed_at: match &row["observed_at"] {
            Value::Null => None,
            v => Some(instant(v)?),
        },
        raw: RawRef {
            sha256: text(&row["raw_sha256"]),
            item: int(&row["raw_item"]),
        },
        decoder_version: int(&row["decoder_version"]),
        facts: facts_from_json(&row["facts"]).map_err(|e| bad(e.0))?,
    })
}

/// The key an `ObservationRef` and an observation row share.
pub fn observation_key(value: &Value) -> (String, String, i64, String) {
    (
        text(&value["source"]),
        text(value.get("sha256").unwrap_or(&value["raw_sha256"])),
        int(value.get("item").unwrap_or(&value["raw_item"])),
        text(&value["received_at"]),
    )
}

fn opt_journey(value: &Value) -> StoreResult<Option<JourneyKey>> {
    if value.is_null() {
        Ok(None)
    } else {
        journey(value).map(Some)
    }
}

fn parsed<T>(value: &Value, parse: fn(&str) -> Option<T>) -> StoreResult<T> {
    parse(value.as_str().unwrap_or_default()).ok_or_else(|| bad(format!("unknown {value}")))
}

/// An `effect` record's effect; observations are found by their reference. `Unresolved` (never
/// stored) gives None.
pub fn effect(
    value: &Value,
    observations: &std::collections::HashMap<(String, String, i64, String), Arc<Observation>>,
) -> StoreResult<Option<Effect>> {
    let e = &value["effect"];
    Ok(Some(match e["@type"].as_str().unwrap_or_default() {
        "ObservationResult" => {
            let key = observation_key(&e["observation"]);
            let observation = observations
                .get(&key)
                .cloned()
                .ok_or_else(|| bad(format!("no observation row for {key:?}")))?;
            Effect::ObservationResult(ObservationResult {
                observation,
                journey: opt_journey(&e["journey"])?,
                reason: match &e["reason"] {
                    Value::Null => None,
                    v => Some(parsed(v, Reason::parse)?),
                },
                public: e["public"]
                    .as_array()
                    .map(Vec::as_slice)
                    .unwrap_or_default()
                    .iter()
                    .map(journey)
                    .collect::<StoreResult<_>>()?,
                state_since: match &e["state_since"] {
                    Value::Null => None,
                    v => Some(instant(v)?),
                },
                method: parsed(&e["method"], BindingMethod::parse)?,
            })
        }
        "SnapshotJourney" => Effect::SnapshotJourney(snapshot(e)?),
        "WriteEvent" => Effect::WriteEvent(WriteEvent {
            journey: journey(&e["journey"])?,
            location_id: text(&e["location_id"]),
            visit_n: u32::try_from(int(&e["visit_n"])).unwrap_or_default(),
            kind: parsed(&e["kind"], EventKind::parse)?,
            interval: Interval::new(
                instant(&e["interval"]["lo"])?,
                instant(&e["interval"]["hi"])?,
            ),
            method: parsed(&e["method"], EventMethod::parse)?,
            source: text(&e["source"]),
        }),
        "AssignVehicle" => Effect::AssignVehicle(AssignVehicle {
            vehicle: VehicleId {
                source: text(&e["vehicle"]["source"]),
                source_vehicle_id: text(&e["vehicle"]["source_vehicle_id"]),
            },
            journey: journey(&e["journey"])?,
            at: instant(&e["at"])?,
            method: parsed(&e["method"], BindingMethod::parse)?,
        }),
        "LinkJourneys" => Effect::LinkJourneys(LinkJourneys {
            source: journey(&e["source"])?,
            target: journey(&e["target"])?,
            kind: parsed(&e["kind"], JourneyLinkKind::parse)?,
        }),
        "RecordPlatform" => {
            let p = &e["platform"];
            Effect::RecordPlatform(RecordPlatform {
                journey: journey(&e["journey"])?,
                location_id: text(&e["location_id"]),
                visit_n: u32::try_from(int(&e["visit_n"])).unwrap_or_default(),
                kind: parsed(&e["kind"], StopKind::parse)?,
                platform: Platform {
                    value: text(&p["value"]),
                    label: parsed(&p["label"], PlatformLabel::parse)?,
                    boarding_point_id: opt_text(&p["boarding_point_id"]),
                    assigned_at: instant(&p["assigned_at"])?,
                    source: text(&p["source"]),
                },
            })
        }
        "RecordCancellation" => Effect::RecordCancellation(RecordCancellation {
            journey: journey(&e["journey"])?,
            source: text(&e["source"]),
            at: instant(&e["at"])?,
            cancelled: e["cancelled"].as_bool().unwrap_or_default(),
        }),
        _ => return Ok(None),
    }))
}
