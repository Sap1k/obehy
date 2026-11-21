//! The release index the core reads: static data per trip key, loaded lazily (R3; the loader logic
//! of `index_sql.py` is [`loader`]).
//!
//! The worker and replay fill an [`Index`] through an [`IndexLoader`] before each step, for keys
//! not seen yet; the rows come from a [`Rows`] implementation in obehy-rt (PostgreSQL `static.*`
//! or a release's serving Parquet). The core treats a key it was not given as a programming
//! error ([`IndexMiss`]).
//!
//! Rail (docs/R2_SLICE.md section 2): a CZPTT path (PA, `trip.run_key`) is published as trip
//! parts ordered by `run_part`, which keep the path's sequence numbers, so consecutive parts
//! share their junction call's sequence. The core follows the whole path as one **run trip**
//! per service date: [`Index::run`] concatenates the parts running that day, merges each
//! junction call and drops non-passenger points without any time. It is synthetic
//! (`run:<PA>:<parts>`) and never output; `Trip::parts` say which calls belong to which part.
//!
//! Memory: every ID is interned once per index ([`pool::Id`]), times are integer seconds, a
//! trip's calls are one flat slice, a shape two flat slices; every map has its eviction rule
//! stated next to it in [`Index`], and [`Index::sweep`] drops in place and shrinks.

pub mod loader;
pub mod pool;
mod store;

use std::sync::Arc;

pub use loader::{
    CallRow, IndexLoader, KeyRow, LoadError, LocationRow, Rows, RowsError, RunPartRow,
    ShapePointRow, TripRow,
};
pub use pool::{Id, Pool};
pub use store::{DateSet, Index, calls_at_station, run_trip};

use crate::time::Date;

/// A source key: (namespace, identifier). Orders, str)` tuple.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct KeyRef {
    pub namespace: Id,
    pub identifier: Id,
}

impl KeyRef {
    pub fn new(namespace: &str, identifier: &str) -> KeyRef {
        KeyRef {
            namespace: Arc::from(namespace),
            identifier: Arc::from(identifier),
        }
    }
}

/// A composite source key's namespace and the namespace of its leading part, so a missing trip
/// can be told apart from a missing line. The split is the source's own key format, never a
/// public ID.
pub const PARENT_NAMESPACE: &[(&str, &str)] = &[("cis:line_trip", "cis:line")];

/// The parent namespace of a composite key namespace.
pub fn parent_namespace(namespace: &str) -> Option<&'static str> {
    PARENT_NAMESPACE
        .iter()
        .find(|(child, _)| *child == namespace)
        .map(|(_, parent)| *parent)
}

/// The reverse, for keyless sources that name only the line: its trips' keys are
/// `<line>:<trip>` in the child namespace.
pub fn child_namespace(namespace: &str) -> Option<&'static str> {
    PARENT_NAMESPACE
        .iter()
        .find(|(_, parent)| *parent == namespace)
        .map(|(child, _)| *child)
}

/// `parent_ref`: the line key of a composite trip key, if it is one.
pub fn parent_ref(key: &KeyRef) -> Option<KeyRef> {
    let parent = parent_namespace(&key.namespace)?;
    let (line, _) = key.identifier.split_once(':')?;
    Some(KeyRef::new(parent, line))
}

/// The core asked for static data that was never loaded.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct IndexMiss(pub String);

/// A source key applies to `public_id` (a trip or a route) on the service dates inside its
/// validity.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct KeyEntry {
    pub public_id: Id,
    pub valid_from: Date,
    pub valid_to: Date,
}

impl KeyEntry {
    pub fn valid_on(&self, day: Date) -> bool {
        self.valid_from <= day && day <= self.valid_to
    }
}

/// A scheduled call; times are seconds after the service date's local midnight.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Call {
    pub sequence: i32,
    pub location_id: Id,
    /// The post (boarding point) it stops at, where its position is known exactly: a stop place
    /// visited at several posts in a row is placed at each of them.
    pub point_id: Option<Id>,
    pub arrival: Option<i32>,
    pub departure: Option<i32>,
    pub visit_n: u32,
    pub passenger_service: bool,
    /// A request stop (pickup or drop-off "coordinate with driver"): the vehicle may pass it.
    pub on_request: bool,
}

impl Call {
    /// The location the call is placed on the path at: its post, else its stop place.
    pub fn placed_at(&self) -> &Id {
        self.point_id.as_ref().unwrap_or(&self.location_id)
    }

    /// The call's representative schedule time (departure, else arrival).
    pub fn time(&self) -> Result<i32, IndexMiss> {
        self.departure.or(self.arrival).ok_or_else(|| {
            IndexMiss(format!(
                "call {} at {} has no time",
                self.sequence, self.location_id
            ))
        })
    }
}

/// A published trip part of a run trip: its calls are `calls[first..=last]` of the run.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TripPart {
    pub trip_id: Id,
    pub train_number: Option<Id>,
    pub first: usize,
    pub last: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Trip {
    pub trip_id: Id,
    pub route_id: Id,
    pub route_name: Id,
    pub mode: Id,
    pub service_id: Id,
    pub headsign: Option<Id>,
    pub shape_id: Option<Id>,
    pub calls: Box<[Call]>,
    /// Rail: the CZPTT path (PA) this trip is a part of, its place in it and its train number.
    pub run_key: Option<Id>,
    pub run_part: Option<i16>,
    pub train_number: Option<Id>,
    /// Only on a run trip: its published parts, in order.
    pub parts: Box<[TripPart]>,
    pub agency_id: Option<Id>,
}

impl Trip {
    /// The first call's departure, else its time. Panics on a trip without calls, as Python's
    /// `calls[0]` raises.
    pub fn start(&self) -> Result<i32, IndexMiss> {
        let first = &self.calls[0];
        first.departure.map_or_else(|| first.time(), Ok)
    }

    /// The last call's arrival, else its time.
    pub fn end(&self) -> Result<i32, IndexMiss> {
        let last = &self.calls[self.calls.len() - 1];
        last.arrival.map_or_else(|| last.time(), Ok)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Location {
    pub location_id: Id,
    pub name: Id,
    pub lon: Option<f64>,
    pub lat: Option<f64>,
}

/// Vertices (lon, lat) and metres travelled at each, computed from the geometry.
#[derive(Debug, Clone, PartialEq)]
pub struct Shape {
    pub shape_id: Id,
    pub points: Box<[(f64, f64)]>,
    pub distances_m: Box<[f64]>,
}
