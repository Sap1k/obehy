//! A journey's timetable laid on its path: trigger points and when the vehicle is due where.
//!
//! Built once per trip and service date ([`PlanCache`]) and read by the tracker and the
//! estimates. `BASE_PLAN.md` sections 20.4 (lateness against the timetable window of a place)
//! and 20.7 (trigger points).
//!
//! Memory: [`PlanCache`] holds paths by trip and plans by (trip, service date). Plans are all
//! dropped once there are more than [`MAX_PLANS`] and everything on a release change;
//! [`PlanCache::sweep`] keeps only the trips the index keeps and plans from a date on (the runner
//! sweeps with the index).

use rustc_hash::FxHashMap as HashMap;
use std::sync::Arc;

use crate::config::Policy;
use crate::config::policy::ProgressPolicy;
use crate::geo::LonLat;
use crate::index::{Id, Index, IndexMiss, Trip};
use crate::model::EventKind;
use crate::time::{Date, Instant, ServiceTime, micros, scaled_micros};
use crate::timeline::path::{Path, bisect_right, build_path};
use crate::timeline::runtimes::HopPlan;

/// Per call: (scheduled arrival, scheduled departure) (`plan.Scheduled`).
pub use crate::timeline::runtimes::Scheduled;

/// Where along the path a call's arrival and departure trigger; None where it has none.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StopZone {
    pub arrival_m: Option<f64>,
    pub departure_m: Option<f64>,
}

/// One event point along the path.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Trigger {
    pub along_m: f64,
    pub call: usize,
    pub kind: EventKind,
}

/// When the timetable has the vehicle at a place; None is unbounded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Window {
    pub earliest: Option<Instant>,
    pub latest: Option<Instant>,
}

impl Window {
    /// The timetable time of being here at `at`: `at` clamped into the window.
    pub fn clamp(&self, at: Instant) -> Instant {
        if let Some(earliest) = self.earliest
            && at < earliest
        {
            return earliest;
        }
        if let Some(latest) = self.latest
            && at > latest
        {
            return latest;
        }
        at
    }
}

/// Each call's trigger points (see plan.py `stop_zones`): arrival `arrival_radius_m` before the
/// stop, departure `departure_margin_m` after it, never past the midpoint to the neighbouring
/// call; calls in a row at one point are one dwell.
pub fn stop_zones(path: &Path, policy: &Policy) -> Vec<StopZone> {
    let radius = policy.lifecycle.arrival_radius_m;
    let margin = policy.lifecycle.departure_margin_m;
    let distances = &path.call_distances_m;
    let last = distances.len() as i64 - 1;
    let mut zones: Vec<StopZone> = Vec::with_capacity(distances.len());
    for (i, &distance) in distances.iter().enumerate() {
        let (mut arrival, mut departure) = (None, None);
        if i > 0 {
            if distances[i - 1] == distance {
                arrival = zones[i - 1].arrival_m;
                if arrival.is_none() {
                    arrival = Some(distance - radius); // visits at the origin
                }
            } else {
                arrival = Some((distance - radius).max((distances[i - 1] + distance) / 2.0));
            }
        }
        if (i as i64) < last {
            if distances[i + 1] == distance {
                departure = Some(arrival.unwrap_or(distance - radius));
            } else {
                departure = Some((distance + margin).min((distance + distances[i + 1]) / 2.0));
            }
        }
        zones.push(StopZone {
            arrival_m: arrival,
            departure_m: departure,
        });
    }
    zones
}

/// A call's stretch of path (between its triggers) and its timetable window there.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Span {
    start_m: f64,
    end_m: f64,
    window: Window,
}

/// When the timetable has the vehicle at a distance along the path (plan.py `Timetable`).
///
/// At a call (between its arrival and departure triggers) it is the dwell, [arrival,
/// departure]; the first call has no earliest time, so waiting at the origin is never early.
/// Between calls it is interpolated by distance from one call's departure trigger to the next
/// call's arrival trigger. None where the timetable has no time.
#[derive(Debug, Clone, PartialEq)]
pub struct Timetable {
    spans: Vec<Span>,
    starts: Vec<f64>,
    /// The windows in exact microseconds, for the tracker's inner loop (`lateness_us`).
    bounds_us: Vec<(Option<i64>, Option<i64>)>,
}

impl Timetable {
    /// Panics unless `scheduled` and `zones` have one entry per call (`zip(strict=True)`).
    pub fn new(path: &Path, scheduled: &[Scheduled], zones: &[StopZone]) -> Timetable {
        assert_eq!(scheduled.len(), zones.len(), "zip(strict=True)");
        let distances = &path.call_distances_m;
        let mut spans: Vec<Span> = Vec::with_capacity(zones.len());
        let mut starts: Vec<f64> = Vec::with_capacity(zones.len());
        for (i, (&(arrival, departure), zone)) in scheduled.iter().zip(zones).enumerate() {
            let mut earliest = if i == 0 { None } else { arrival.or(departure) };
            let latest = departure.or(arrival);
            if i > 0 && distances[i - 1] == distances[i] {
                // Visits in a row at one point are one dwell: it starts with the first (none at
                // the origin).
                earliest = spans[spans.len() - 1].window.earliest;
            }
            let start = zone.arrival_m.unwrap_or(f64::NEG_INFINITY);
            let end = zone.departure_m.unwrap_or(f64::INFINITY);
            spans.push(Span {
                start_m: start,
                end_m: end,
                window: Window { earliest, latest },
            });
            starts.push(start);
        }
        let bounds_us = spans
            .iter()
            .map(|s| (s.window.earliest.map(micros), s.window.latest.map(micros)))
            .collect();
        Timetable {
            spans,
            starts,
            bounds_us,
        }
    }

    /// `max(0, bisect_right(starts, along_m) - 1)`
    fn span_at(&self, along_m: f64) -> usize {
        bisect_right(&self.starts, along_m).saturating_sub(1)
    }

    pub fn window(&self, along_m: f64) -> Option<Window> {
        let spans = &self.spans;
        if spans.is_empty() {
            return None;
        }
        let i = self.span_at(along_m);
        let span = spans[i];
        if along_m <= span.end_m || i == spans.len() - 1 {
            if along_m >= span.start_m {
                return Some(span.window);
            }
            return Some(Window {
                earliest: None,
                latest: span.window.latest,
            });
        }
        let (leave, reach) = (span.window.latest?, spans[i + 1].window.earliest?);
        let gap = spans[i + 1].start_m - span.end_m;
        let share = if gap <= 0.0 {
            0.0
        } else {
            (along_m - span.end_m) / gap
        };
        let planned = leave + (reach - leave).mul_f64(share);
        Some(Window {
            earliest: Some(planned),
            latest: Some(planned),
        })
    }

    /// Seconds behind (positive) or ahead of the timetable at a place; 0 inside a dwell.
    pub fn lateness_s(&self, at: Instant, along_m: f64) -> f64 {
        self.lateness_us(micros(at), along_m)
    }

    /// `lateness_s` with the time in microseconds, for the tracker's inner loop: the one
    /// implementation of the rule.
    pub fn lateness_us(&self, at_us: i64, along_m: f64) -> f64 {
        let spans = &self.spans;
        if spans.is_empty() {
            return 0.0;
        }
        let i = self.span_at(along_m);
        let span = spans[i];
        let (earliest, latest);
        if along_m <= span.end_m || i == spans.len() - 1 {
            let (e, l) = self.bounds_us[i];
            earliest = if along_m < span.start_m { None } else { e };
            latest = l;
        } else {
            let (Some(leave), Some(reach)) = (self.bounds_us[i].1, self.bounds_us[i + 1].0) else {
                return 0.0;
            };
            let gap = spans[i + 1].start_m - span.end_m;
            let share = if gap <= 0.0 {
                0.0
            } else {
                (along_m - span.end_m) / gap
            };
            let planned = leave + scaled_micros(reach - leave, share);
            earliest = Some(planned);
            latest = Some(planned);
        }
        // int / int true division: exact operands (below 2^53), one correctly rounded division.
        if let Some(earliest) = earliest
            && at_us < earliest
        {
            return (at_us - earliest) as f64 / 1e6;
        }
        if let Some(latest) = latest
            && at_us > latest
        {
            return (at_us - latest) as f64 / 1e6;
        }
        0.0
    }

    /// The timetable time of being at a place at `at`.
    pub fn planned(&self, at: Instant, along_m: f64) -> Option<Instant> {
        self.window(along_m).map(|w| w.clamp(at))
    }
}

/// A call the timetable gives no dwell: arrival and departure in the same minute.
pub fn no_timetabled_dwell(scheduled: Scheduled) -> bool {
    let (arrival, departure) = scheduled;
    arrival.is_some() && arrival == departure
}

/// A journey's path with its timetable laid on it.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub path: Arc<Path>,
    pub scheduled: Vec<Scheduled>,
    pub zones: Vec<StopZone>,
    pub timetable: Timetable,
    /// In path order.
    pub triggers: Vec<Trigger>,
    pub trigger_distances: Vec<f64>,
}

impl Plan {
    /// Triggers with `after_m < along_m <= upto_m`, in path order.
    pub fn triggers_between(&self, after_m: f64, upto_m: f64) -> &[Trigger] {
        let first = bisect_right(&self.trigger_distances, after_m);
        let last = bisect_right(&self.trigger_distances, upto_m);
        // Python's slice of first > last is empty.
        if first >= last {
            return &[];
        }
        &self.triggers[first..last]
    }

    /// Where the vehicle has left the first stop: its departure trigger, else the stop.
    pub fn departure_m(&self) -> f64 {
        let first = self.zones.first().and_then(|z| z.departure_m);
        first.unwrap_or_else(|| self.path.call_distances_m[0])
    }

    /// Where the vehicle has left the call: its departure trigger (the last call's: its
    /// arrival trigger).
    pub fn leaving_m(&self, call: usize) -> Option<f64> {
        let zone = self.zones[call];
        zone.departure_m.or(zone.arrival_m)
    }

    /// The last call the vehicle has left at `along_m` (`leaving_m` at or behind it); -1
    /// before the first departure.
    pub fn call_index_at(&self, along_m: f64) -> i64 {
        let mut index = -1;
        for i in 0..self.zones.len() {
            if let Some(trigger) = self.leaving_m(i)
                && trigger <= along_m
            {
                index = i as i64;
            }
        }
        index
    }

    /// Whether `along_m` lies at a call, between its arrival and departure triggers (at the
    /// origin: anywhere before its departure trigger).
    pub fn at_stop(&self, along_m: f64) -> bool {
        for zone in &self.zones {
            if let Some(arrival) = zone.arrival_m
                && along_m < arrival
            {
                return false;
            }
            match zone.departure_m {
                None => return true,
                Some(departure) if along_m < departure => return true,
                Some(_) => {}
            }
        }
        false
    }

    /// The call the timetable gives no dwell whose stop zone holds `along_m`; None elsewhere.
    pub fn dwelling_call(&self, along_m: f64) -> Option<usize> {
        for (call, zone) in self.zones.iter().enumerate() {
            if let Some(arrival) = zone.arrival_m
                && along_m < arrival
            {
                return None;
            }
            let inside = match zone.departure_m {
                None => true,
                Some(departure) => along_m < departure,
            };
            if inside {
                return no_timetabled_dwell(self.scheduled[call]).then_some(call);
            }
        }
        None
    }

    /// The call's scheduled time of the event, falling back to its other time.
    pub fn scheduled_time(&self, call: usize, kind: EventKind) -> Option<Instant> {
        let (arrival, departure) = self.scheduled[call];
        if kind == EventKind::Arrival {
            arrival.or(departure)
        } else {
            departure.or(arrival)
        }
    }
}

impl HopPlan for Plan {
    fn scheduled(&self, call: usize) -> Scheduled {
        self.scheduled[call]
    }

    fn arrival_m(&self, call: usize) -> Option<f64> {
        self.zones[call].arrival_m
    }

    fn departure_m(&self, call: usize) -> Option<f64> {
        self.zones[call].departure_m
    }

    fn call_distance_m(&self, call: usize) -> f64 {
        self.path.call_distances_m[call]
    }
}

/// Each call's (arrival, departure) instants on service date `day`.
pub fn scheduled_instants(trip: &Trip, day: Date) -> Vec<Scheduled> {
    trip.calls
        .iter()
        .map(|c| {
            (
                c.arrival.map(|s| ServiceTime::new(day, s).instant()),
                c.departure.map(|s| ServiceTime::new(day, s).instant()),
            )
        })
        .collect()
}

pub fn build_plan(path: Arc<Path>, scheduled: Vec<Scheduled>, policy: &Policy) -> Plan {
    let zones = stop_zones(&path, policy);
    let mut triggers: Vec<Trigger> = Vec::with_capacity(zones.len() * 2);
    for (i, zone) in zones.iter().enumerate() {
        for (kind, along) in [
            (EventKind::Arrival, zone.arrival_m),
            (EventKind::Departure, zone.departure_m),
        ] {
            if let Some(along_m) = along {
                triggers.push(Trigger {
                    along_m,
                    call: i,
                    kind,
                });
            }
        }
    }
    // sorted(key=(along_m, call, kind != "arrival")): stable, float keys by partial_cmp.
    triggers.sort_by(|a, b| {
        a.along_m
            .partial_cmp(&b.along_m)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.call.cmp(&b.call))
            .then((a.kind != EventKind::Arrival).cmp(&(b.kind != EventKind::Arrival)))
    });
    let trigger_distances = triggers.iter().map(|t| t.along_m).collect();
    let timetable = Timetable::new(&path, &scheduled, &zones);
    Plan {
        path,
        scheduled,
        zones,
        timetable,
        triggers,
        trigger_distances,
    }
}

/// path.py `build_path(trip, index, policy)`: the trip's calls placed at their posts' or stop
/// places' coordinates, on its shape if it has one. Errors where Python raises `IndexMiss`
/// (a location or shape not loaded, a call without a time), in Python's order.
pub fn trip_path(trip: &Trip, index: &Index, policy: &ProgressPolicy) -> Result<Path, IndexMiss> {
    let mut located: Vec<Option<LonLat>> = Vec::with_capacity(trip.calls.len());
    for call in trip.calls.iter() {
        let location = index.location(call.placed_at())?;
        located.push(match (location.lon, location.lat) {
            (Some(lon), Some(lat)) => Some((lon, lat)),
            _ => None,
        });
    }
    let shape = match &trip.shape_id {
        Some(shape_id) => Some(index.shape(shape_id)?.clone()),
        None => None,
    };
    build_path(
        &located,
        shape
            .as_deref()
            .map(|s| (&s.points[..], &s.distances_m[..])),
        |i| trip.calls[i].time().map(i64::from),
        policy,
    )
}

/// Plans held before all are dropped: plan.py's module constant `50_000`, kept as is. (By
/// AGENTS.md it belongs in policy; Python is frozen for the port, so it moves with both.)
pub const MAX_PLANS: usize = 50_000;

/// Paths by trip and plans by journey for one release; deterministic, so caching never changes
/// output.
///
/// Eviction: every plan when there are more than [`MAX_PLANS`] before an insertion; everything
/// when the index's release changes; [`PlanCache::sweep`] for the rest. Paths have no limit of
/// their own between sweeps.
#[derive(Debug, Default)]
pub struct PlanCache {
    release_id: String,
    paths: HashMap<Id, Arc<Path>>,
    plans: HashMap<(Id, Date), Arc<Plan>>,
}

impl PlanCache {
    pub fn new() -> PlanCache {
        PlanCache::default()
    }

    /// The plan of `trip` on service date `day`, built and cached on first use.
    pub fn plan(
        &mut self,
        trip: &Trip,
        day: Date,
        index: &Index,
        policy: &Policy,
    ) -> Result<Arc<Plan>, IndexMiss> {
        if index.release_id != self.release_id {
            self.release_id.clone_from(&index.release_id);
            self.paths.clear();
            self.plans.clear();
        }
        let key = (trip.trip_id.clone(), day);
        if let Some(plan) = self.plans.get(&key) {
            return Ok(plan.clone());
        }
        let path = match self.paths.get(&trip.trip_id) {
            Some(path) => path.clone(),
            None => {
                let path = Arc::new(trip_path(trip, index, &policy.progress)?);
                self.paths.insert(trip.trip_id.clone(), path.clone());
                path
            }
        };
        let plan = Arc::new(build_plan(path, scheduled_instants(trip, day), policy));
        if self.plans.len() > MAX_PLANS {
            self.plans.clear();
        }
        self.plans.insert(key, plan.clone());
        Ok(plan)
    }

    /// The plan of `trip` on `day` if it is cached for `index`'s release (what [`plan`] would
    /// return without building anything).
    ///
    /// [`plan`]: PlanCache::plan
    pub fn cached(&self, trip: &Trip, day: Date, index: &Index) -> Option<Arc<Plan>> {
        if index.release_id != self.release_id {
            return None;
        }
        self.plans.get(&(trip.trip_id.clone(), day)).cloned()
    }

    /// Keep the paths of the trips `keep` names and their plans from `since` on.
    pub fn sweep(&mut self, keep: impl Fn(&str) -> bool, since: Date) {
        self.paths.retain(|trip_id, _| keep(trip_id));
        self.plans
            .retain(|(trip_id, day), _| keep(trip_id) && *day >= since);
        self.paths.shrink_to_fit();
        self.plans.shrink_to_fit();
    }

    pub fn release_id(&self) -> &str {
        &self.release_id
    }

    /// (paths, plans) held, for the worker's container sizes.
    pub fn sizes(&self) -> (usize, usize) {
        (self.paths.len(), self.plans.len())
    }
}
