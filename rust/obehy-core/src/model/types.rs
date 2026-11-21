//! Core realtime types (docs/R1_SLICE.md section 1): identity, instance state, effects, as
//! plain data.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt;
use std::sync::Arc;

use indexmap::IndexMap;

use super::facts::*;

/// An `IndexMap` with a fast hasher: iteration follows insertion, never the hash.
pub type FastIndexMap<K, V> = IndexMap<K, V, rustc_hash::FxBuildHasher>;
use crate::time::{Date, Instant};

/// A vehicle as one source names it; ordered by (source, id).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct VehicleId {
    pub source: String,
    pub source_vehicle_id: String,
}

impl fmt::Display for VehicleId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.source, self.source_vehicle_id)
    }
}

/// The public and history identity of an operating instance (BASE_PLAN.md section 29),
/// ordered by (feed, namespace, key, service date).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct JourneyKey {
    pub feed: Feed,
    pub namespace: String,
    pub key: String,
    pub service_date: Date,
}

/// Each call's visit number within a sequence of calls: 1 at a location's first call, 2 at its
/// second...
pub fn visit_numbers<'a>(location_ids: impl IntoIterator<Item = &'a str>) -> Vec<u32> {
    let mut seen: std::collections::HashMap<&str, u32> = std::collections::HashMap::new();
    location_ids
        .into_iter()
        .map(|id| {
            let n = seen.entry(id).or_insert(0);
            *n += 1;
            *n
        })
        .collect()
}

literal! {
    /// Why an observation is not (or no longer) bound to a journey.
    Reason {
        NoKey = "no_key",
        NoLine = "no_line",
        NoTrip = "no_trip",
        NotActive = "not_active",
        NotInService = "not_in_service",
        Ambiguous = "ambiguous",
        StaleKey = "stale_key",
    }
}

literal! {
    BindingMethod { Keyed = "keyed", Inferred = "inferred" }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Binding {
    pub vehicle: VehicleId,
    pub journey: JourneyKey,
    pub trip_id: String,
    pub method: BindingMethod,
    pub bound_at: Instant,
    /// An inferred binding's stop events go to history only once continuity confirms it.
    pub confirmed: bool,
}

/// A keyless vehicle left the stop named `stop` at `left_at` (ARRIVA-Q2).
#[derive(Debug, Clone, PartialEq)]
pub struct Departure {
    pub stop: String,
    pub left_at: Instant,
    pub scheduled: Option<Interval>,
    pub position: Option<Position>,
}

/// What a keyless vehicle has shown since its line and destination last changed.
#[derive(Debug, Clone, PartialEq)]
pub struct KeylessEpisode {
    pub line: String,
    pub destination: String,
    /// The source's stop names may leave out their leading parts (MPVNET-Q10).
    pub townless: bool,
    pub started_at: Instant,
    pub last_at: Instant,
    pub next_stop: Option<String>,
    pub departures: Vec<Departure>,
}

literal! {
    Lifecycle {
        Forecast = "forecast",
        PreTrip = "pre_trip",
        Running = "running",
        Finished = "finished",
        Cancelled = "cancelled",
    }
}

literal! {
    CallStatus {
        Scheduled = "scheduled",
        Predicted = "predicted",
        Actual = "actual",
        Inferred = "inferred",
        NoRealtime = "no_realtime",
        Cancelled = "cancelled",
    }
}

literal! {
    SourceClass { Gps = "gps", Source = "source" }
}

/// The platform or track a source assigned to a call (docs/R2_SLICE.md section 6).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Platform {
    pub value: String,
    pub label: PlatformLabel,
    pub boarding_point_id: Option<String>,
    pub assigned_at: Instant,
    pub source: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct CallState {
    pub sequence: i64,
    pub location_id: String,
    pub visit_n: u32,
    pub arrival: Option<Interval>,
    pub departure: Option<Interval>,
    pub estimated_arrival: Option<Instant>,
    pub estimated_departure: Option<Instant>,
    pub status: CallStatus,
    pub source_class: Option<SourceClass>,
    /// When progress crossed the call's triggers (realtime only).
    pub passed_arrival: Option<Instant>,
    pub passed_departure: Option<Instant>,
    /// Railway points that are not passenger stops are tracked but never published.
    pub passenger: bool,
    pub platform: Option<Platform>,
}

impl CallState {
    /// A call with the dataclass defaults.
    pub fn new(sequence: i64, location_id: impl Into<String>, visit_n: u32) -> CallState {
        CallState {
            sequence,
            location_id: location_id.into(),
            visit_n,
            arrival: None,
            departure: None,
            estimated_arrival: None,
            estimated_departure: None,
            status: CallStatus::Scheduled,
            source_class: None,
            passed_arrival: None,
            passed_departure: None,
            passenger: true,
            platform: None,
        }
    }
}

/// Distance along the journey's path and the last call reached or passed.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Progress {
    pub distance_m: f64,
    pub call_index: i64,
    pub at: Instant,
}

/// Where a reading placed one fix along the path.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Placement {
    pub along_m: f64,
    pub at: Instant,
}

/// One reading of where the vehicle is along the path (BASE_PLAN.md section 20.4).
#[derive(Debug, Clone, PartialEq)]
pub struct Hypothesis {
    pub along_m: f64,
    pub at: Instant,
    pub lateness_s: f64,
    pub log_p: f64,
    /// Each fix since the commit point, oldest first.
    pub history: Arc<[Placement]>,
    /// `at` in exact microseconds.
    pub at_us: i64,
    pub off_path_since: Option<Instant>,
    pub reckoned_m: f64,
}

/// The estimate along a stop-to-stop chord while the fixes are off it (lateness only).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct OffPath {
    pub along_m: f64,
    pub at: Instant,
    pub lateness_s: f64,
}

/// The fixes at a call whose departure is not committed yet (`timeline.stops`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StopSeen {
    pub call: i64,
    pub first: Instant,
    pub last: Instant,
    pub away: Option<Instant>,
    pub away_m: Option<f64>,
}

/// The newest fix's source and place (lon, lat).
#[derive(Debug, Clone, PartialEq)]
pub struct Fix {
    pub source: String,
    pub lon: f64,
    pub lat: f64,
}

/// The live hypotheses of a journey and how far events are committed.
#[derive(Debug, Clone, PartialEq)]
pub struct Track {
    pub hypotheses: Vec<Hypothesis>,
    pub committed_m: f64,
    pub unmatched_since: Option<Instant>,
    pub seen_at: Option<Instant>,
    pub crossed_at: Option<Instant>,
    pub off_path: Option<OffPath>,
    pub fix: Option<Fix>,
    pub at_stop: Option<StopSeen>,
}

impl Track {
    /// The measured lateness: off a chord the estimate, else the best reading's.
    pub fn lateness_s(&self) -> Option<f64> {
        match &self.off_path {
            Some(off) => Some(off.lateness_s),
            None => self.hypotheses.first().map(|h| h.lateness_s),
        }
    }
}

/// How current a journey's data is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Freshness {
    pub updated_at: Instant,
    pub heard_at: Instant,
    pub stale: bool,
    pub lost: bool,
}

/// The one vehicle whose observations drive the timeline (DUK-Q11).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Lead {
    pub vehicle: VehicleId,
    pub seen: Instant,
}

/// A public journey of a run instance: the run's calls `first..=last`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct JourneySpan {
    pub journey: JourneyKey,
    pub first: usize,
    pub last: usize,
}

/// A published trip part of a run instance: the run's calls `first..=last`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PartSpan {
    pub trip_id: String,
    pub first: usize,
    pub last: usize,
}

/// What one source has said about a journey (fusion, docs/R2_SLICE.md section 5).
#[derive(Debug, Clone, PartialEq)]
pub struct SourceTrack {
    pub source: String,
    pub heard_at: Instant,
    pub facts: Option<Arc<[Fact]>>,
    pub next_point: Option<NextPoint>,
    pub delay_s: Option<i64>,
    pub delay_at: Option<Instant>,
    pub delay_reference: Option<DelayReference>,
    pub prediction: Option<NextStopPrediction>,
    pub point: Option<i64>,
    pub position_at: Option<Instant>,
    pub lead: Option<Lead>,
}

/// Where a journey's vehicle is shown.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Published {
    pub lon: f64,
    pub lat: f64,
    pub bearing: Option<f64>,
    pub at: Instant,
    pub along_m: Option<f64>,
    pub snapped: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Instance {
    pub journey: JourneyKey,
    pub release_id: String,
    pub trip_id: String,
    /// The route mode, for per-mode policy values.
    pub mode: String,
    pub lifecycle: Lifecycle,
    pub calls: Arc<[CallState]>,
    pub freshness: Freshness,
    pub progress: Option<Progress>,
    pub track: Option<Track>,
    pub reported_delay_s: Option<i64>,
    pub lateness_s: Option<i64>,
    pub off_route_since: Option<Instant>,
    pub off_route: bool,
    pub lead: Option<Lead>,
    pub journeys: Vec<JourneySpan>,
    pub parts: Vec<PartSpan>,
    pub sources: Vec<SourceTrack>,
    pub track_source: Option<String>,
    pub shown_source: Option<String>,
    pub published: Option<Published>,
    pub held: Vec<WriteEvent>,
}

impl Instance {
    pub fn source_track(&self, source: &str) -> Option<&SourceTrack> {
        self.sources.iter().find(|track| track.source == source)
    }

    /// The lifecycle once the calls' events have changed (`Instance.settled_lifecycle`).
    pub fn settled_lifecycle(&self, placed: bool) -> Lifecycle {
        let arrived = self
            .calls
            .last()
            .is_some_and(|c| c.passed_arrival.is_some());
        if arrived && matches!(self.lifecycle, Lifecycle::PreTrip | Lifecycle::Running) {
            return Lifecycle::Finished;
        }
        if placed && self.lifecycle == Lifecycle::PreTrip {
            return Lifecycle::Running;
        }
        self.lifecycle
    }

    /// The last call the vehicle has left; -1 before it left the first.
    pub fn left_index(&self) -> i64 {
        let mut left = self.progress.map_or(-1, |p| p.call_index);
        for (i, call) in self.calls.iter().enumerate() {
            let i = i as i64;
            if call.passed_departure.is_some() {
                left = left.max(i);
            } else if call.passed_arrival.is_some() {
                left = left.max(i - 1);
            }
        }
        left
    }

    /// The last call the vehicle has reached (arrived at or left); -1 before the first.
    pub fn reached_index(&self) -> i64 {
        let mut reached = self.progress.map_or(-1, |p| p.call_index);
        for (i, call) in self.calls.iter().enumerate() {
            if call.passed_arrival.is_some() || call.passed_departure.is_some() {
                reached = reached.max(i as i64);
            }
        }
        reached
    }

    /// The vehicle leading one source's contributions (DUK-Q11 within a source).
    pub fn lead_for(&self, source: &str) -> Option<&Lead> {
        if let Some(lead) = &self.lead
            && lead.vehicle.source == source
        {
            return Some(lead);
        }
        self.source_track(source)
            .and_then(|track| track.lead.as_ref())
    }

    /// The public journey and visit number of call `index` for an event of `kind`.
    pub fn public_call(&self, index: usize, kind: EventKind) -> (JourneyKey, u32) {
        let call = &self.calls[index];
        let spans: Vec<&JourneySpan> = self
            .journeys
            .iter()
            .filter(|s| s.first <= index && index <= s.last)
            .collect();
        let Some(first) = spans.first() else {
            return (self.journey.clone(), call.visit_n);
        };
        let span = if kind == EventKind::Arrival {
            first
        } else {
            spans[spans.len() - 1]
        };
        let visits = visit_numbers(
            self.calls[span.first..=index]
                .iter()
                .map(|c| c.location_id.as_str()),
        );
        (
            span.journey.clone(),
            *visits.last().expect("at least the call itself"),
        )
    }

    /// The public journeys: the spans', or the instance's own.
    pub fn public(&self) -> Vec<JourneyKey> {
        if self.journeys.is_empty() {
            vec![self.journey.clone()]
        } else {
            self.journeys.iter().map(|s| s.journey.clone()).collect()
        }
    }
}

literal! {
    VehicleStatus {
        Running = "running",
        Positioning = "positioning",
        Layover = "layover",
        Unmatched = "unmatched",
        NotInService = "not_in_service",
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct VehicleState {
    pub vehicle: VehicleId,
    pub feed: Feed,
    pub status: VehicleStatus,
    pub last_seen: Instant,
    pub trip_key: Option<TripKey>,
    pub binding: Option<Binding>,
    pub position: Option<Position>,
    pub position_at: Option<Instant>,
    pub reason: Option<Reason>,
    pub episode: Option<KeylessEpisode>,
    pub facts: Arc<[Fact]>,
    pub since: Option<Instant>,
}

/// A hop between consecutive passenger calls: (mode, from, to, timed from the first call's
/// departure).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct HopKey {
    pub mode: String,
    pub from: String,
    pub to: String,
    pub from_departure: bool,
}

/// One vehicle's traversal of a hop: (arrival at its end, seconds taken, seconds scheduled).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Traversal {
    pub end: Instant,
    pub taken_s: f64,
    pub scheduled_s: f64,
}

/// One feed's live state; `core.step` updates it in place.
#[derive(Debug, Clone)]
pub struct FeedState {
    pub feed: Feed,
    pub vehicles: BTreeMap<VehicleId, VehicleState>,
    pub instances: BTreeMap<JourneyKey, Instance>,
    /// Journeys whose estimates are out of date (only ever iterated sorted).
    pub dirty: BTreeSet<JourneyKey>,
    /// Recent traversals of each hop, oldest first (insertion order kept).
    pub hops: FastIndexMap<HopKey, VecDeque<Traversal>>,
}

impl FeedState {
    pub fn new(feed: Feed) -> FeedState {
        FeedState {
            feed,
            vehicles: BTreeMap::new(),
            instances: BTreeMap::new(),
            dirty: BTreeSet::new(),
            hops: FastIndexMap::default(),
        }
    }
}

// --- effects ---------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ScheduledCall {
    pub ordinal: i64,
    pub location_id: String,
    pub visit_n: u32,
    pub passenger_service: bool,
    pub scheduled_arrival: Option<i32>,
    pub scheduled_departure: Option<i32>,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SnapshotJourney {
    pub journey: JourneyKey,
    pub at: Instant,
    pub release_id: String,
    pub trip_id: String,
    pub route_name: String,
    pub headsign: Option<String>,
    pub calls: Arc<[ScheduledCall]>,
    pub run_key: Option<String>,
    pub route_id: Option<String>,
    pub mode: Option<String>,
    pub agency_id: Option<String>,
}

literal! {
    JourneyLinkKind { ContinuesAs = "continues_as", SplitsFrom = "splits_from", Joins = "joins" }
}

/// Two public journeys of one run: the train continues under a new number.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct LinkJourneys {
    pub source: JourneyKey,
    pub target: JourneyKey,
    pub kind: JourneyLinkKind,
}

/// A platform assignment for history (`history.platform_evidence`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RecordPlatform {
    pub journey: JourneyKey,
    pub location_id: String,
    pub visit_n: u32,
    pub kind: StopKind,
    pub platform: Platform,
}

literal! {
    /// How an event was established: the source said so, crossed between close fixes, or
    /// inferred across a gap.
    EventMethod { Source = "source", Progress = "progress", Inferred = "inferred" }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct WriteEvent {
    pub journey: JourneyKey,
    pub location_id: String,
    pub visit_n: u32,
    pub kind: EventKind,
    pub interval: Interval,
    pub method: EventMethod,
    pub source: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AssignVehicle {
    pub vehicle: VehicleId,
    pub journey: JourneyKey,
    pub at: Instant,
    pub method: BindingMethod,
}

/// A public journey cancelled by `source` at `at`, or (`cancelled` false) seen running.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RecordCancellation {
    pub journey: JourneyKey,
    pub source: String,
    pub at: Instant,
    pub cancelled: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ObservationResult {
    pub observation: Arc<Observation>,
    pub journey: Option<JourneyKey>,
    pub reason: Option<Reason>,
    pub public: Vec<JourneyKey>,
    pub state_since: Option<Instant>,
    pub method: BindingMethod,
}

literal! {
    UnresolvedKind { Point = "point", Inconsistent = "inconsistent", Platform = "platform", Behind = "behind" }
}

/// Diagnostics: a source named something the run could not place; never stored.
#[derive(Debug, Clone, PartialEq)]
pub struct Unresolved {
    pub observation: Arc<Observation>,
    pub kind: UnresolvedKind,
    pub name: String,
}

/// What the core asks the shell to do.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    SnapshotJourney(SnapshotJourney),
    WriteEvent(WriteEvent),
    AssignVehicle(AssignVehicle),
    ObservationResult(ObservationResult),
    LinkJourneys(LinkJourneys),
    RecordPlatform(RecordPlatform),
    RecordCancellation(RecordCancellation),
    Unresolved(Unresolved),
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Derivation {
    pub core_version: String,
    pub policy_version: String,
    pub release_id: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(n: &str) -> JourneyKey {
        JourneyKey {
            feed: Feed::Czptt,
            namespace: "czptt:train_number".into(),
            key: n.into(),
            service_date: Date::ymd(2026, 10, 8),
        }
    }

    fn instance(calls: Vec<CallState>) -> Instance {
        let at = Instant::from_micros(0);
        Instance {
            journey: key("run"),
            release_id: "r".into(),
            trip_id: "t".into(),
            mode: "rail".into(),
            lifecycle: Lifecycle::Running,
            calls: calls.into(),
            freshness: Freshness {
                updated_at: at,
                heard_at: at,
                stale: false,
                lost: false,
            },
            progress: None,
            track: None,
            reported_delay_s: None,
            lateness_s: None,
            off_route_since: None,
            off_route: false,
            lead: None,
            journeys: Vec::new(),
            parts: Vec::new(),
            sources: Vec::new(),
            track_source: None,
            shown_source: None,
            published: None,
            held: Vec::new(),
        }
    }

    #[test]
    fn visits_and_passed_calls() {
        assert_eq!(visit_numbers(["A", "B", "A", "A"]), vec![1, 1, 2, 3]);
        let at = Some(Instant::from_micros(1));
        let mut calls: Vec<CallState> = ["A", "B", "C", "D"]
            .iter()
            .enumerate()
            .map(|(i, l)| CallState::new(i as i64, *l, 1))
            .collect();
        let mut inst = instance(calls.clone());
        assert_eq!((inst.left_index(), inst.reached_index()), (-1, -1));
        calls[0].passed_departure = at;
        calls[2].passed_arrival = at;
        inst.calls = calls.clone().into();
        assert_eq!((inst.left_index(), inst.reached_index()), (1, 2));
        assert_eq!(inst.settled_lifecycle(false), Lifecycle::Running);
        calls[3].passed_arrival = at;
        inst.calls = calls.into();
        assert_eq!(inst.settled_lifecycle(false), Lifecycle::Finished);
    }

    #[test]
    fn a_shared_call_belongs_to_both_journeys() {
        let calls: Vec<CallState> = ["A", "B", "C"]
            .iter()
            .enumerate()
            .map(|(i, l)| CallState::new(i as i64, *l, 1))
            .collect();
        let mut inst = instance(calls);
        inst.journeys = vec![
            JourneySpan {
                journey: key("1"),
                first: 0,
                last: 1,
            },
            JourneySpan {
                journey: key("2"),
                first: 1,
                last: 2,
            },
        ];
        assert_eq!(inst.public_call(1, EventKind::Arrival), (key("1"), 1));
        assert_eq!(inst.public_call(1, EventKind::Departure), (key("2"), 1));
        assert_eq!(inst.public(), vec![key("1"), key("2")]);
    }
}
