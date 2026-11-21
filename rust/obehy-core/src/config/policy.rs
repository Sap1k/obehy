//! Versioned realtime policy (`src/obehy/data/realtime/policy-v18.toml`): port of
//! `realtime/policy.py`. Every value is required (a missing key is a load error); the sections
//! Python reads field by field reject unknown keys, as Python does; `[delay]`, `[warm_replay]`,
//! `[emit]` and the root take extra keys silently, as Python does.
//!
//! Names follow the TOML: Python's flattened `policy.delay_discard_below_s` is
//! `policy.delay.discard_below_s`, `warm_replay_mode` is `warm_replay.mode`, `emit_tick_s` is
//! `emit.tick_s` (and `sweep_index_every_s`, `keep_index_s`, `compact_index_at_s` live in
//! `emit`). Per-mode values ([`ByMode`]) are floats, as Python's are.

use std::collections::BTreeMap;

use serde::Deserialize;

use super::ConfigError;
use crate::time::TimeDelta;

/// The policy file of this code (Python's `POLICY`).
pub const POLICY_FILE: &str = "policy-v18.toml";

/// A value with per-route-mode (or per-source) overrides: a table with `default`.
#[derive(Debug, Clone, PartialEq)]
pub struct ByMode {
    pub default: f64,
    /// Sorted by name.
    pub overrides: Vec<(String, f64)>,
}

impl ByMode {
    pub fn get(&self, mode: &str) -> f64 {
        self.overrides
            .iter()
            .find(|(name, _)| name == mode)
            .map_or(self.default, |(_, v)| *v)
    }

    /// The largest value of any mode.
    pub fn longest(&self) -> f64 {
        self.overrides.iter().fold(self.default, |most, (_, v)| {
            crate::py::num::py_max(most, *v)
        })
    }

    /// `timedelta(seconds=value)` for a mode (CPython's float rounding).
    pub fn delta(&self, mode: &str) -> TimeDelta {
        TimeDelta::from_secs_f64(self.get(mode))
    }
}

impl<'de> Deserialize<'de> for ByMode {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<ByMode, D::Error> {
        let mut table = BTreeMap::<String, f64>::deserialize(deserializer)?;
        let default = table
            .remove("default")
            .ok_or_else(|| serde::de::Error::missing_field("default"))?;
        Ok(ByMode {
            default,
            overrides: table.into_iter().collect(),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JourneyPolicy {
    /// A trip's journey key: the first of these it has.
    pub namespaces: Vec<String>,
    /// Keys naming one journey whichever source reports them (a subset of `namespaces`).
    pub shared: Vec<String>,
}

impl JourneyPolicy {
    /// (namespace, key) of the journey of a trip with these keys (namespace -> key).
    pub fn key_of<'a>(&'a self, keys: &'a BTreeMap<String, String>) -> Option<(&'a str, &'a str)> {
        self.namespaces
            .iter()
            .find_map(|ns| keys.get(ns).map(|key| (ns.as_str(), key.as_str())))
    }

    pub fn is_shared(&self, namespace: &str) -> bool {
        self.shared.iter().any(|ns| ns == namespace)
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TimePolicy {
    pub pre_trip_s: ByMode,
    pub max_delay_s: ByMode,
    pub vehicle_day_gap_s: i64,
    pub max_clock_skew_s: i64,
}

impl TimePolicy {
    pub fn max_clock_skew(&self) -> TimeDelta {
        TimeDelta::from_seconds(self.max_clock_skew_s)
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LifecyclePolicy {
    pub stale_after_s: ByMode,
    pub predict_without_data_s: ByMode,
    pub arrival_radius_m: f64,
    pub departure_margin_m: f64,
    pub terminal_radius_m: f64,
    pub publish_finished_s: ByMode,
    pub forget_after_s: i64,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProgressPolicy {
    pub gps_sigma_m: f64,
    pub shape_sigma_m: f64,
    pub chord_min_sigma_m: f64,
    pub chord_k: f64,
    pub chord_max_sigma_m: f64,
    pub reach_sigmas: f64,
    pub bearing_sigma_deg: f64,
    pub chord_bearing_sigma_deg: f64,
    pub jitter_m: f64,
    pub off_path_log_p: f64,
    pub max_speed_mps: ByMode,
    pub loss_sigma_base_s: f64,
    pub loss_sigma_rate: f64,
    pub gain_sigma_base_s: f64,
    pub gain_sigma_rate: f64,
    pub start_lateness_sigma_s: f64,
    pub start_early_sigma_s: f64,
    pub beam: i64,
    pub prune: f64,
    pub agree_within: f64,
    pub max_commit_lag_s: i64,
    pub max_event_interval_s: i64,
    pub max_inferred_spread_s: i64,
    pub max_extrapolate_s: i64,
    pub off_route_hold_s: i64,
    pub move_sigma_m: f64,
    pub move_slack_m: f64,
    pub reckon_slack: f64,
    pub standing_m: f64,
    pub stop_ahead_calls: i64,
    pub stop_off_shape_m: f64,
    pub shape_simplify_deg: f64,
    pub merge_m: f64,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PredictionPolicy {
    pub min_dwell_s: ByMode,
    pub long_dwell_s: ByMode,
    pub min_long_dwell_s: ByMode,
    pub max_early_departure_s: ByMode,
    pub runtime_decay_s: i64,
    pub runtime_prior_weight: f64,
    pub runtime_ratio_max: f64,
    pub runtime_keep_decays: i64,
    pub publish_deadband_s: ByMode,
    pub floor_fresh_s: ByMode,
    pub dwell_credit_s: ByMode,
    pub early_keep: ByMode,
}

/// `[delay]`.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct DelayPolicy {
    pub discard_below_s: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WarmReplayMode {
    Vehicles,
    Window,
}

/// `[warm_replay]`.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct WarmReplayPolicy {
    #[serde(deserialize_with = "warm_replay_mode")]
    pub mode: WarmReplayMode,
    pub max_hours: i64,
}

fn warm_replay_mode<'de, D: serde::Deserializer<'de>>(d: D) -> Result<WarmReplayMode, D::Error> {
    match String::deserialize(d)?.as_str() {
        "vehicles" => Ok(WarmReplayMode::Vehicles),
        "window" => Ok(WarmReplayMode::Window),
        other => Err(serde::de::Error::custom(format!(
            "warm_replay.mode must be vehicles or window, not {other:?}"
        ))),
    }
}

/// `[emit]`.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct EmitPolicy {
    pub tick_s: i64,
    pub sweep_index_every_s: i64,
    pub keep_index_s: i64,
    pub compact_index_at_s: i64,
    /// Rust only (Python's loader takes extra `[emit]` keys and never forgets): the vehicle
    /// book forgets a report no observation named for this long, checked every
    /// `sweep_index_every_s` (`VehicleBook::forget_unseen`); absent: never.
    #[serde(default)]
    pub forget_reports_s: Option<i64>,
}

/// How long stored realtime data is kept (`obehy jobs hourly` and `nightly`).
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetentionPolicy {
    pub observation_hours: i64,
    pub raw_days: ByMode,
    pub history_days: i64,
}

impl RetentionPolicy {
    pub fn longest_raw_days(&self) -> i64 {
        self.raw_days.longest() as i64
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GuardPolicy {
    pub free_gb: i64,
    pub min_raw_days: i64,
    pub min_history_days: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EgressPolicy {
    pub refresh_s: i64,
    pub cooldown_s: i64,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BoardPolicy {
    pub min_boarding_points: i64,
    pub window_min: i64,
    pub due_refresh_s: i64,
    pub early_min: i64,
    pub near_min: i64,
    pub near_refresh_s: i64,
    pub imminent_min: i64,
    pub imminent_refresh_s: i64,
    pub ceiling_per_min: i64,
    pub slowdown_s: i64,
    pub slow_latency_factor: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Predictor {
    AnchorChange,
    Sz,
    Propagate,
}

impl Predictor {
    pub const ALL: [(&'static str, Predictor); 3] = [
        ("anchor_change", Predictor::AnchorChange),
        ("sz", Predictor::Sz),
        ("propagate", Predictor::Propagate),
    ];

    pub fn as_str(self) -> &'static str {
        Predictor::ALL
            .iter()
            .find(|(_, p)| *p == self)
            .map(|(name, _)| *name)
            .expect("listed")
    }
}

fn predictor<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Predictor, D::Error> {
    let name = String::deserialize(d)?;
    Predictor::ALL
        .iter()
        .find(|(n, _)| *n == name)
        .map(|(_, p)| *p)
        .ok_or_else(|| {
            serde::de::Error::custom("rail.predictor must be one of anchor_change, sz, propagate")
        })
}

/// Fusing several sources on one rail run (docs/R2_SLICE.md section 5).
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RailPolicy {
    pub position_preference: Vec<String>,
    pub position_fresh_s: i64,
    pub position_sigma_m: ByMode,
    pub event_tolerance_s: i64,
    #[serde(deserialize_with = "predictor")]
    pub predictor: Predictor,
}

impl RailPolicy {
    /// A source's rank in `position_preference`; unlisted sources rank last.
    pub fn rank(&self, source: &str) -> usize {
        self.position_preference
            .iter()
            .position(|s| s == source)
            .unwrap_or(self.position_preference.len())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeylessPolicy {
    pub candidate_window_s: i64,
    pub episode_gap_s: i64,
    pub accept_s: i64,
    pub gps_accept_s: i64,
    pub margin_s: i64,
    pub max_path_distance_m: f64,
    pub departure_radius_m: f64,
    pub confirm_departures: i64,
}

/// The whole policy file.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Policy {
    #[serde(rename = "policy_version")]
    pub version: String,
    pub journey: JourneyPolicy,
    pub time: TimePolicy,
    pub lifecycle: LifecyclePolicy,
    pub progress: ProgressPolicy,
    pub prediction: PredictionPolicy,
    pub delay: DelayPolicy,
    pub warm_replay: WarmReplayPolicy,
    pub emit: EmitPolicy,
    pub retention: RetentionPolicy,
    pub guard: GuardPolicy,
    pub egress: EgressPolicy,
    pub rail: RailPolicy,
    pub boards: BoardPolicy,
    pub keyless: KeylessPolicy,
}

fn names_ok(names: &[String]) -> bool {
    names.iter().all(|n| !n.is_empty())
}

impl Policy {
    /// `parse_policy` of a TOML document.
    pub fn from_toml(text: &str) -> Result<Policy, ConfigError> {
        let policy: Policy =
            toml::from_str(text).map_err(|e| ConfigError::Policy(e.to_string()))?;
        policy.validate()?;
        Ok(policy)
    }

    /// `load_policy(path)`.
    pub fn load(path: &std::path::Path) -> Result<Policy, ConfigError> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| ConfigError::Policy(format!("{}: {e}", path.display())))?;
        Policy::from_toml(&text)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        let fail = |message: &str| Err(ConfigError::Policy(message.to_owned()));
        if self.version.is_empty() {
            return fail("policy_version must be a non-empty string");
        }
        let journey = &self.journey;
        if journey.namespaces.is_empty() || !names_ok(&journey.namespaces) {
            return fail("journey.namespaces must be a non-empty list of names");
        }
        if journey.shared.is_empty() || !names_ok(&journey.shared) {
            return fail("journey.shared must be a non-empty list of names");
        }
        if !journey
            .shared
            .iter()
            .all(|s| journey.namespaces.contains(s))
        {
            return fail("journey.shared must be journey namespaces");
        }
        if self.lifecycle.publish_finished_s.longest() >= self.lifecycle.forget_after_s as f64 {
            return fail("lifecycle.publish_finished_s must be below forget_after_s");
        }
        if !names_ok(&self.rail.position_preference) {
            return fail("rail.position_preference must be a list of source names");
        }
        Ok(())
    }
}
