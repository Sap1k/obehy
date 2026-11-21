//! Progress along a trip's path as map matching over a beam of hypotheses. Port of
//! `realtime/timeline/progress.py`, operation for operation (its docstring has the model).
//!
//! `BASE_PLAN.md` section 20.4 and `docs/R1_SLICE.md` section 9. Each fix extends every live
//! hypothesis to each place on the path it may be (one candidate per segment), scored by the
//! geometry fit, forward speed and the change of lateness; a fix the path does not explain holds
//! a hypothesis at a fixed cost. The most likely hypothesis is the live position; crossings are
//! committed once the readings agree (`commit`).
//!
//! Float comparisons that decide (each on operands computed as Python computes them, so they
//! decide alike; the fixtures check every hypothesis bit for bit): `moved <= standing_m`,
//! `origin <= committed_m`, the backward test `candidate < along - jitter - slack * reckoned`,
//! `elapsed <= 0`, `max(0, moved) - jitter > speed * elapsed`, `change >= 0`, `short > 0`, the
//! Viterbi `score > best` (strict: the first live hypothesis wins ties), the prune
//! `top - log_p > prune` and merge `|a - b| < merge_m`, the sort by (-log_p, along, held), the
//! history's chained equality, and in `at_origin_or_moving` `along <= departure`,
//! `a - b > jitter`, `lateness >= earliest`.

use std::sync::Arc;

use crate::config::policy::ProgressPolicy;
use crate::geo::{LonLat, distance_m, square};
use crate::model::{
    EventKind, Fix, Hypothesis, OffPath, Placement, Position, Progress, StopSeen, Track,
};
use crate::py::num::{py_fmod, py_hypot, py_max, py_min};
use crate::time::{Instant, TimeDelta, micros};
use crate::timeline::commit::{Crossing, commit};
use crate::timeline::path::{Candidate, Path, candidates, point_at};
use crate::timeline::plan::Plan;
use crate::timeline::stops::{capped, done_at, seen};

/// The tracker after one fix.
#[derive(Debug, Clone, PartialEq)]
pub struct Step {
    pub track: Track,
    pub progress: Option<Progress>,
    pub crossed: Vec<Crossing>,
    pub off_route_since: Option<Instant>,
    pub off_route: bool,
}

/// Python's float `/`: panics on a zero divisor, where Python raises `ZeroDivisionError`.
fn div(a: f64, b: f64) -> f64 {
    assert!(b != 0.0, "float division by zero");
    a / b
}

fn empty_track() -> Track {
    Track {
        hypotheses: Vec::new(),
        committed_m: 0.0,
        unmatched_since: None,
        seen_at: None,
        crossed_at: None,
        off_path: None,
        fix: None,
        at_stop: None,
    }
}

/// Extend the journey's hypotheses with one fix (from `source`) and commit what they agree on
/// (progress.py `update`). Fixes within `stop_radius_m` of a stop, or within `terminal_m` of the
/// first or last stop while the vehicle stands, bound its events there (`stops`); a fix at a
/// stop without a timetabled dwell stands for `dwell_s` there. `prior` is the vehicle's last fix
/// before this one, whatever the journey made of it.
#[allow(clippy::too_many_arguments)]
pub fn update(
    plan: &Plan,
    track: Option<&Track>,
    position: &Position,
    at: Instant,
    mode: &str,
    source: &str,
    policy: &ProgressPolicy,
    dwell_s: f64,
    stop_radius_m: f64,
    terminal_m: f64,
    prior: Option<&Position>,
) -> Step {
    if plan.path.points.len() < 2 {
        return Step {
            track: track.cloned().unwrap_or_else(empty_track),
            progress: None,
            crossed: Vec::new(),
            off_route_since: None,
            off_route: false,
        };
    }
    let here: LonLat = (position.lon, position.lat);
    // How far the fixes moved apart; only between fixes of one source.
    let mut moved: Option<f64> = None;
    if let Some(fix) = track.and_then(|t| t.fix.as_ref())
        && fix.source == source
    {
        moved = Some(distance_m((fix.lon, fix.lat), here));
    } else if let Some(prior) = prior {
        moved = Some(distance_m((prior.lon, prior.lat), here));
    }
    let mut stop: Option<StopSeen> = None;
    if stop_radius_m > 0.0 {
        // Before anything is matched too: a stand off the path is still the first stop.
        let standing = moved.is_some_and(|m| m <= policy.standing_m);
        stop = seen(
            plan,
            track.and_then(|t| t.at_stop.as_ref()),
            match track {
                Some(t) if !t.hypotheses.is_empty() => t.committed_m,
                _ => 0.0,
            },
            here,
            at,
            stop_radius_m,
            policy.stop_ahead_calls,
            if standing { terminal_m } else { 0.0 },
            if standing {
                track.and_then(|t| t.seen_at)
            } else {
                None
            },
        );
    }
    let live: &[Hypothesis] = track.map_or(&[], |t| &t.hypotheses);
    let speed = policy.max_speed_mps.get(mode);
    let at_us = micros(at);
    let window = search_window(live, at_us, speed, policy);
    let found = candidates(
        &plan.path,
        position.lon,
        position.lat,
        sigma(policy),
        policy.reach_sigmas,
        window,
    );
    let extended = if live.is_empty() {
        start(plan, &found, position, at, at_us, policy)
    } else {
        let mut out = extend(
            plan, live, &found, position, at, at_us, speed, moved, policy,
        );
        out.extend(held(plan, live, at, at_us, moved, here, policy));
        out
    };
    let fix = Fix {
        source: source.to_owned(),
        lon: here.0,
        lat: here.1,
    };
    if extended.is_empty() {
        return unmatched(track, at, policy, fix, stop);
    }

    let beam = prune(extended, policy);
    // A restart never commits behind what was committed.
    let floor = first_floor(&beam);
    let mut committed_m = match track {
        Some(t) if !live.is_empty() => t.committed_m,
        _ => floor,
    };
    if let Some(t) = track
        && live.is_empty()
    {
        committed_m = py_max(t.committed_m, floor);
    }
    // Nothing committed yet: the first reading may start past the origin.
    let unstarted = track.is_none_or(|t| t.hypotheses.is_empty() && t.committed_m == 0.0);
    let origin = if unstarted {
        plan.zones.first().and_then(|z| z.departure_m)
    } else {
        None
    };
    if let Some(origin) = origin
        && origin <= committed_m
    {
        // A new track first seen past the origin: its departure may still be inferred.
        committed_m = origin.next_down();
    }
    let crossed_at = track.and_then(|t| t.crossed_at);
    if let Some(s) = stop.as_mut()
        && s.away == Some(at)
        && s.away_m.is_none()
    {
        s.away_m = Some(beam[0].along_m);
    }
    // The first fix back on the path after a stretch off it: a departure it crossed waits.
    let returning = live.first().is_some_and(|h| h.off_path_since.is_some());
    let done = commit(
        plan,
        beam,
        committed_m,
        crossed_at,
        at,
        policy,
        dwell_s,
        stop.as_ref(),
        returning,
    );
    let best = done.beam[0].clone();
    if let Some(s) = stop
        && done_at(plan, s.call as usize, done.committed_m)
    {
        stop = None;
    }
    let along = capped(plan, stop.as_ref(), best.along_m);
    let progress = Progress {
        distance_m: along,
        call_index: plan.call_index_at(along),
        at,
    };
    let since = best.off_path_since;
    let off_route =
        since.is_some_and(|s| at - s >= TimeDelta::from_seconds(policy.off_route_hold_s));
    let previous = track.and_then(|t| t.off_path);
    let estimate = chord_estimate(plan, &best, previous, here, at, at_us, speed);
    let new_track = Track {
        hypotheses: done.beam,
        committed_m: done.committed_m,
        unmatched_since: None,
        seen_at: Some(at),
        crossed_at: done.crossed_at,
        off_path: estimate,
        fix: Some(fix),
        at_stop: stop,
    };
    Step {
        track: new_track,
        progress: Some(progress),
        crossed: done.crossed,
        off_route_since: since,
        off_route,
    }
}

/// Whether a fix shows the vehicle at its first stop (before the departure trigger, or standing
/// within `terminal_m` of it), or two consecutive fixes show it moving on along the path after
/// it, no further ahead of the timetable than the start prior allows (progress.py
/// `at_origin_or_moving`).
pub fn at_origin_or_moving(
    plan: &Plan,
    before: Option<&Position>,
    after: &Position,
    at: Instant,
    policy: &ProgressPolicy,
    terminal_m: f64,
) -> bool {
    let after_m = plan.departure_m();
    let now = candidates(
        &plan.path,
        after.lon,
        after.lat,
        sigma(policy),
        policy.reach_sigmas,
        None,
    );
    if now.iter().any(|a| a.along_m <= after_m) {
        return true;
    }
    if let Some(before) = before
        && terminal_m > 0.0
        && now.is_empty()
    {
        // Standing off the path near the first stop: a terminal's stand.
        let (here, then) = ((after.lon, after.lat), (before.lon, before.lat));
        let origin = point_at(&plan.path, plan.path.call_distances_m[0]);
        if distance_m(here, then) <= policy.standing_m && distance_m(here, origin) <= terminal_m {
            return true;
        }
    }
    let Some(before) = before else {
        return false;
    };
    let then = candidates(
        &plan.path,
        before.lon,
        before.lat,
        sigma(policy),
        policy.reach_sigmas,
        None,
    );
    let earliest = -policy.reach_sigmas * policy.start_early_sigma_s;
    then.iter().any(|b| {
        now.iter().any(|a| {
            b.along_m > after_m
                && a.along_m - b.along_m > policy.jitter_m
                && plan.timetable.lateness_s(at, a.along_m) >= earliest
        })
    })
}

/// Whether the fix lies on the path after the first stop's departure trigger.
pub fn past_first_stop(plan: &Plan, position: &Position, policy: &ProgressPolicy) -> bool {
    let after = plan.departure_m();
    candidates(
        &plan.path,
        position.lon,
        position.lat,
        sigma(policy),
        policy.reach_sigmas,
        None,
    )
    .iter()
    .any(|c| c.along_m > after)
}

/// `min(values)`: the first of equal values; panics when empty, as Python raises.
fn fold_min(mut values: impl Iterator<Item = f64>) -> f64 {
    let first = values.next().expect("min() of an empty sequence");
    values.fold(first, py_min)
}

/// `max(values)`: the first of equal values; panics when empty.
fn fold_max(mut values: impl Iterator<Item = f64>) -> f64 {
    let first = values.next().expect("max() of an empty sequence");
    values.fold(first, py_max)
}

/// Where commits start for a new track: nothing before the first fix was observed.
fn first_floor(beam: &[Hypothesis]) -> f64 {
    fold_min(beam.iter().map(|h| h.along_m))
}

// --- extending hypotheses ---------------------------------------------------------------------

/// The stretch of path any live hypothesis could have reached since its last fix; the whole
/// path when there is none.
fn search_window(
    live: &[Hypothesis],
    at_us: i64,
    speed: f64,
    policy: &ProgressPolicy,
) -> Option<(f64, f64)> {
    if live.is_empty() {
        return None;
    }
    let elapsed = fold_max(live.iter().map(|h| (at_us - h.at_us) as f64 / 1e6));
    let reach = policy.reach_sigmas * py_hypot(&[policy.gps_sigma_m, policy.chord_min_sigma_m]);
    Some((
        fold_min(live.iter().map(|h| h.along_m)) - policy.jitter_m,
        fold_max(live.iter().map(|h| h.along_m)) + speed * py_max(0.0, elapsed) + reach,
    ))
}

/// The first fix: one hypothesis per candidate, with a prior on absolute lateness that is
/// broad for running late and narrow for running early.
fn start(
    plan: &Plan,
    found: &[Candidate],
    position: &Position,
    at: Instant,
    at_us: i64,
    policy: &ProgressPolicy,
) -> Vec<Hypothesis> {
    let mut out = Vec::with_capacity(found.len());
    for candidate in found {
        let lateness = plan.timetable.lateness_us(at_us, candidate.along_m);
        let sigma = if lateness < 0.0 {
            policy.start_early_sigma_s
        } else {
            policy.start_lateness_sigma_s
        };
        let prior = -0.5 * square(div(lateness, sigma));
        let fit = emission(candidate, position, &plan.path, policy);
        out.push(Hypothesis {
            along_m: candidate.along_m,
            at,
            lateness_s: lateness,
            log_p: fit + prior,
            history: Arc::from([Placement {
                along_m: candidate.along_m,
                at,
            }]),
            at_us,
            off_path_since: None,
            reckoned_m: 0.0,
        });
    }
    out
}

/// Each candidate continues the live hypothesis that reaches it most plausibly (Viterbi).
#[allow(clippy::too_many_arguments)]
fn extend(
    plan: &Plan,
    live: &[Hypothesis],
    found: &[Candidate],
    position: &Position,
    at: Instant,
    at_us: i64,
    speed: f64,
    moved: Option<f64>,
    policy: &ProgressPolicy,
) -> Vec<Hypothesis> {
    let timetable = &plan.timetable;
    let jitter = policy.jitter_m;
    // On a real shape a reading must keep up with how far the fixes moved apart.
    let shortfall = match moved {
        Some(moved) if plan.path.shaped => Some(moved - policy.move_slack_m),
        _ => None,
    };
    let move_sigma = policy.move_sigma_m;
    // Python memoizes the lateness per place (`late`); it is pure, so it is computed here.
    let mut out = Vec::with_capacity(found.len());
    for candidate in found {
        let fit = emission(candidate, position, &plan.path, policy);
        let mut best: Option<(f64, &Hypothesis, f64, f64)> = None; // score, from, along, late
        for h in live {
            // Backwards beyond GPS noise is impossible.
            if candidate.along_m < h.along_m - jitter - policy.reckon_slack * h.reckoned_m {
                continue;
            }
            // A small step back is no movement, except to correct an overshoot.
            let along = if h.reckoned_m > 0.0 {
                candidate.along_m
            } else {
                py_max(candidate.along_m, h.along_m)
            };
            let lateness = timetable.lateness_us(at_us, along);
            let Some(mut step) = transition(h, along, at_us, lateness, speed, policy) else {
                continue;
            };
            if let Some(shortfall) = shortfall
                && h.off_path_since.is_none()
            {
                let short = shortfall - (along - h.along_m);
                if short > 0.0 {
                    step -= 0.5 * square(div(short, move_sigma));
                }
            }
            let score = h.log_p + step + fit;
            if best.is_none_or(|b| score > b.0) {
                best = Some((score, h, along, lateness));
            }
        }
        if let Some((score, h, along, lateness)) = best {
            out.push(Hypothesis {
                along_m: along,
                at,
                lateness_s: lateness,
                log_p: score,
                history: placed(&h.history, along, at),
                at_us,
                off_path_since: None,
                reckoned_m: 0.0,
            });
        }
    }
    out
}

/// `history` with a fix at `along_m`; of a run of fixes at one place only the first and the
/// last are kept.
fn placed(history: &[Placement], along_m: f64, at: Instant) -> Arc<[Placement]> {
    let placement = Placement { along_m, at };
    let n = history.len();
    let mut out: Vec<Placement> = Vec::with_capacity(n + 1);
    // `history[-1].along_m == along_m == history[-2].along_m`
    if n >= 2 && history[n - 1].along_m == along_m && along_m == history[n - 2].along_m {
        out.extend_from_slice(&history[..n - 1]);
    } else {
        out.extend_from_slice(history);
    }
    out.push(placement);
    Arc::from(out)
}

/// A fix the geometry does not explain: each hypothesis may also hold, at a fixed cost (see
/// progress.py `_held`). On a real shape it advances by the distance the fixes moved, never into
/// the next call's arrival trigger, and never gains time.
fn held(
    plan: &Plan,
    live: &[Hypothesis],
    at: Instant,
    at_us: i64,
    moved: Option<f64>,
    here: LonLat,
    policy: &ProgressPolicy,
) -> Vec<Hypothesis> {
    let mut out = Vec::with_capacity(live.len());
    for h in live {
        let (mut along, mut lateness) = (h.along_m, h.lateness_s);
        if let Some(moved) = moved
            && plan.path.shaped
        {
            let (trigger, call) = next_arrival(plan, h.along_m);
            let at_call = plan.path.call_distances_m[call];
            let left = distance_m(here, point_at(&plan.path, at_call));
            along = py_max(
                h.along_m,
                py_min(py_min(h.along_m + moved, trigger - 1.0), at_call - left),
            );
            lateness = py_max(plan.timetable.lateness_us(at_us, along), h.lateness_s);
        }
        out.push(Hypothesis {
            along_m: along,
            at: h.at,
            lateness_s: lateness,
            log_p: h.log_p + policy.off_path_log_p,
            history: h.history.clone(),
            at_us: h.at_us,
            off_path_since: Some(h.off_path_since.unwrap_or(at)),
            reckoned_m: h.reckoned_m + (along - h.along_m),
        });
    }
    out
}

/// While the best reading holds off a stop-to-stop chord: where the fix puts the vehicle on
/// its leg, bounded as progress.py `_chord_estimate` bounds it. Lateness only.
fn chord_estimate(
    plan: &Plan,
    best: &Hypothesis,
    previous: Option<OffPath>,
    here: LonLat,
    at: Instant,
    at_us: i64,
    speed: f64,
) -> Option<OffPath> {
    if plan.path.shaped || best.off_path_since.is_none() || best.at >= at {
        return None;
    }
    let mut floor = best.along_m;
    if let Some(previous) = previous
        && previous.along_m >= floor
    {
        floor = previous.along_m;
    }
    let (trigger, _) = next_arrival(plan, best.along_m);
    let ceiling = py_min(
        trigger,
        best.along_m + speed * ((at_us - best.at_us) as f64 / 1e6),
    );
    let along = py_min(
        py_max(leg_share(plan, best.along_m, here), floor),
        py_max(floor, ceiling),
    );
    let mut lateness = py_max(plan.timetable.lateness_s(at, along), best.lateness_s);
    if let Some(previous) = previous {
        lateness = py_max(lateness, previous.lateness_s);
    }
    Some(OffPath {
        along_m: along,
        at,
        lateness_s: lateness,
    })
}

/// Where a fix puts the vehicle on its leg: between the last call left and the next one by its
/// share of distance to the two.
fn leg_share(plan: &Plan, along_m: f64, here: LonLat) -> f64 {
    let calls = &plan.path.call_distances_m;
    let last = plan.call_index_at(along_m).max(0) as usize;
    if last + 1 >= calls.len() {
        return along_m;
    }
    let (start, end) = (calls[last], calls[last + 1]);
    let to_last = distance_m(here, point_at(&plan.path, start));
    let to_next = distance_m(here, point_at(&plan.path, end));
    let share = if to_last + to_next <= 0.0 {
        1.0
    } else {
        to_last / (to_last + to_next)
    };
    start + share * (end - start)
}

/// The first arrival trigger ahead of `along_m` and its call; past the last, the end.
fn next_arrival(plan: &Plan, along_m: f64) -> (f64, usize) {
    for trigger in plan.triggers_between(along_m, f64::INFINITY) {
        if trigger.kind == EventKind::Arrival {
            return (trigger.along_m, trigger.call);
        }
    }
    let calls = &plan.path.call_distances_m;
    assert!(!calls.is_empty(), "list index out of range");
    let last = calls.len() - 1;
    (calls[last] + 1.0, last)
}

/// Most likely first, scores relative to it; near-identical positions merged; at most `beam`,
/// none more than `prune` behind.
fn prune(mut extended: Vec<Hypothesis>, policy: &ProgressPolicy) -> Vec<Hypothesis> {
    // sorted(key=(-log_p, along_m, off_path_since is not None)): stable, floats by partial_cmp
    // (-0.0 and 0.0 equal, as Python's tuple comparison has them).
    let order = |a: f64, b: f64| a.partial_cmp(&b).unwrap_or(std::cmp::Ordering::Equal);
    extended.sort_by(|a, b| {
        order(-a.log_p, -b.log_p)
            .then(order(a.along_m, b.along_m))
            .then(a.off_path_since.is_some().cmp(&b.off_path_since.is_some()))
    });
    let top = extended[0].log_p;
    let mut kept: Vec<Hypothesis> = Vec::new();
    for h in extended {
        if top - h.log_p > policy.prune || kept.len() as i64 >= policy.beam {
            break;
        }
        if kept
            .iter()
            .any(|k| (h.along_m - k.along_m).abs() < policy.merge_m)
        {
            continue;
        }
        let log_p = h.log_p - top;
        kept.push(Hypothesis { log_p, ..h });
    }
    kept
}

/// A fix before any hypothesis exists that matches no segment: nothing to track yet, but where
/// it is and whether it stands at a terminal.
fn unmatched(
    track: Option<&Track>,
    at: Instant,
    policy: &ProgressPolicy,
    fix: Fix,
    stop: Option<StopSeen>,
) -> Step {
    let track = track.cloned().unwrap_or_else(empty_track);
    let since = track.unmatched_since.unwrap_or(at);
    let held = at - since >= TimeDelta::from_seconds(policy.off_route_hold_s);
    let kept = Track {
        unmatched_since: Some(since),
        seen_at: Some(at),
        fix: Some(fix),
        at_stop: stop,
        ..track
    };
    Step {
        track: kept,
        progress: None,
        crossed: Vec::new(),
        off_route_since: Some(since),
        off_route: held,
    }
}

// --- scoring ----------------------------------------------------------------------------------

/// How far a fix may lie from a segment (one standard deviation): GPS error combined with the
/// trust of the geometry there (progress.py `_sigma`).
pub fn sigma(policy: &ProgressPolicy) -> impl Fn(&Path, f64) -> f64 + '_ {
    let shaped = py_hypot(&[policy.gps_sigma_m, policy.shape_sigma_m]);
    move |path: &Path, segment_m: f64| {
        if path.shaped {
            return shaped;
        }
        let chord = py_max(policy.chord_min_sigma_m, policy.chord_k * segment_m);
        py_hypot(&[policy.gps_sigma_m, py_min(policy.chord_max_sigma_m, chord)])
    }
}

/// Log-likelihood of the fix at the candidate: lateral fit, and heading if it has one.
fn emission(
    candidate: &Candidate,
    position: &Position,
    path: &Path,
    policy: &ProgressPolicy,
) -> f64 {
    let mut score =
        -0.5 * square(div(candidate.lateral_m, candidate.sigma_m)) - candidate.sigma_m.ln();
    if let Some(bearing) = position.bearing {
        let turn = (py_fmod(bearing - candidate.heading_deg + 180.0, 360.0) - 180.0).abs();
        let spread = if path.shaped {
            policy.bearing_sigma_deg
        } else {
            policy.chord_bearing_sigma_deg
        };
        score -= 0.5 * square(div(turn, spread));
    }
    score
}

/// Log-likelihood of moving from `h` to `along` at `at_us`; None if impossible.
fn transition(
    h: &Hypothesis,
    along: f64,
    at_us: i64,
    lateness_s: f64,
    speed: f64,
    policy: &ProgressPolicy,
) -> Option<f64> {
    let moved = along - h.along_m;
    let elapsed = (at_us - h.at_us) as f64 / 1e6;
    if elapsed <= 0.0 {
        return None;
    }
    if py_max(0.0, moved) - policy.jitter_m > speed * elapsed {
        return None;
    }
    let change = lateness_s - h.lateness_s;
    let spread = if change >= 0.0 {
        policy.loss_sigma_base_s + policy.loss_sigma_rate * elapsed
    } else {
        policy.gain_sigma_base_s + policy.gain_sigma_rate * elapsed
    };
    Some(-0.5 * square(div(change, spread)))
}
