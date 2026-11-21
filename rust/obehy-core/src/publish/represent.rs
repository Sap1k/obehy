//! Which vehicles stand for their journey (BASE_PLAN.md section 30). One marker per journey on the
//! map, as GTFS-RT publishes one vehicle per journey.
//!
//! A bus reported by several sources (DÚK and a city's own feed) stands once, as the vehicles
//! of the source shown (`shown_source`: kept while its fixes are fresh). A rail run is one train
//! however many vehicles report it: the shown source's leading vehicle stands for it, else any
//! bound one. The vehicle standing is drawn where the tracker reads the journey from every
//! source's fixes (`Instance.published`, [`shown`]). A vehicle bound to no journey, or to one
//! without an instance, stands for itself.

use std::collections::BTreeSet;

use indexmap::IndexMap;

use crate::config::policy::LifecyclePolicy;
use crate::model::{
    CallStatus, FeedState, Instance, JourneyKey, Lifecycle, Published, VehicleId, VehicleState,
};
use crate::time::{Instant, TimeDelta};

/// The lifecycles of a journey being driven (`LIVE`).
pub fn is_live(lifecycle: Lifecycle) -> bool {
    matches!(lifecycle, Lifecycle::PreTrip | Lifecycle::Running)
}

/// Calls with an observed or inferred crossing (`MADE`).
pub fn is_made(status: CallStatus) -> bool {
    matches!(status, CallStatus::Actual | CallStatus::Inferred)
}

/// Whether and how a journey is published (`Publication`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Publication {
    Live,
    Lost,
    Finished,
    Cancelled,
}

impl Publication {
    pub const fn as_str(self) -> &'static str {
        match self {
            Publication::Live => "live",
            Publication::Lost => "lost",
            Publication::Finished => "finished",
            Publication::Cancelled => "cancelled",
        }
    }
}

/// Whether and how a journey is published at `now`, in GTFS-RT and the state tables alike:
/// live (or lost: no news for long), cancelled, or finished for `publish_finished_s` after its
/// last observed call; None: not (yet, or any more).
pub fn publication(
    instance: &Instance,
    now: Instant,
    policy: &LifecyclePolicy,
) -> Option<Publication> {
    if is_live(instance.lifecycle) {
        return Some(if instance.freshness.lost {
            Publication::Lost
        } else {
            Publication::Live
        });
    }
    match instance.lifecycle {
        Lifecycle::Cancelled => return Some(Publication::Cancelled),
        Lifecycle::Finished => {}
        _ => return None,
    }
    let end = instance
        .calls
        .iter()
        .filter(|c| is_made(c.status))
        .flat_map(|c| [c.estimated_arrival, c.estimated_departure])
        .flatten()
        .max()
        .unwrap_or(instance.freshness.updated_at);
    let keep = TimeDelta::from_secs_f64(policy.publish_finished_s.get(&instance.mode));
    (now - end <= keep).then_some(Publication::Finished)
}

fn shown_source(instance: &Instance) -> Option<&str> {
    instance
        .shown_source
        .as_deref()
        .or(instance.track_source.as_deref())
}

/// Where `vehicle`, standing for its journey, is drawn: the journey's published reading when
/// it is the shown source's lead (or that source's only say) and the journey is not stale;
/// None: at its own fix.
pub fn shown<'a>(instance: Option<&'a Instance>, vehicle: &VehicleId) -> Option<&'a Published> {
    let instance = instance?;
    let published = instance.published.as_ref()?;
    if instance.freshness.stale || Some(vehicle.source.as_str()) != shown_source(instance) {
        return None;
    }
    match instance.lead_for(&vehicle.source) {
        Some(lead) if lead.vehicle != *vehicle => None,
        _ => Some(published),
    }
}

/// The vehicle that stands for the run: the lead of the shown source, else that source's first
/// vehicle, else the first with a position.
pub fn run_position<'a>(
    instance: &Instance,
    bound: &[&'a VehicleState],
) -> Option<&'a VehicleState> {
    let placed: Vec<&'a VehicleState> = bound
        .iter()
        .copied()
        .filter(|v| v.position.is_some())
        .collect();
    if let Some(source) = shown_source(instance) {
        if let Some(lead) = instance.lead_for(source)
            && let Some(found) = placed.iter().find(|c| c.vehicle == lead.vehicle)
        {
            return Some(found);
        }
        if let Some(found) = placed.iter().find(|c| c.vehicle.source == source) {
            return Some(found);
        }
    }
    placed.first().copied()
}

/// The vehicles that stand for a journey (not a rail run): those of one source. The shown
/// source is chosen (`shown_source`), else the first source with a position, else the least
/// source; several vehicles of one source stay (DUK-Q11).
pub fn one_source<'a>(
    instance: &Instance,
    vehicles: &[&'a VehicleId],
    state: &FeedState,
) -> Vec<&'a VehicleId> {
    let sources: BTreeSet<&str> = vehicles.iter().map(|v| v.source.as_str()).collect();
    if sources.len() <= 1 {
        return vehicles.to_vec();
    }
    let placed: BTreeSet<&str> = vehicles
        .iter()
        .filter(|v| state.vehicles[**v].position.is_some())
        .map(|v| v.source.as_str())
        .collect();
    let source = match shown_source(instance) {
        Some(chosen) if placed.contains(chosen) => chosen,
        _ => placed
            .first()
            .or(sources.first())
            .copied()
            .expect("at least two sources"),
    };
    vehicles
        .iter()
        .copied()
        .filter(|v| v.source == source)
        .collect()
}

/// The bound vehicles of each journey, in vehicle order; journeys in the order of their first
/// vehicle (GTFS-RT's vehicle positions follow it).
pub fn vehicles_by_journey(state: &FeedState) -> IndexMap<&JourneyKey, Vec<&VehicleId>> {
    let mut sorted: Vec<(&VehicleId, &VehicleState)> = state.vehicles.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(b.0));
    let mut out: IndexMap<&JourneyKey, Vec<&VehicleId>> = IndexMap::new();
    for (vehicle, current) in sorted {
        if let Some(binding) = &current.binding {
            out.entry(&binding.journey).or_default().push(vehicle);
        }
    }
    out
}

/// The feed's vehicles that stand for their journey, and those bound to none.
pub fn representing(state: &FeedState) -> BTreeSet<&VehicleId> {
    let mut chosen: BTreeSet<&VehicleId> = state
        .vehicles
        .iter()
        .filter(|(_, current)| current.binding.is_none())
        .map(|(vehicle, _)| vehicle)
        .collect();
    for (journey, vehicles) in vehicles_by_journey(state) {
        match state.instances.get(journey) {
            None => chosen.extend(vehicles),
            Some(instance) if !instance.parts.is_empty() => {
                let bound: Vec<&VehicleState> =
                    vehicles.iter().map(|v| &state.vehicles[*v]).collect();
                match run_position(instance, &bound) {
                    Some(lead) => {
                        chosen.insert(&lead.vehicle);
                    }
                    None => chosen.extend(vehicles),
                }
            }
            Some(instance) => chosen.extend(one_source(instance, &vehicles, state)),
        }
    }
    chosen
}
