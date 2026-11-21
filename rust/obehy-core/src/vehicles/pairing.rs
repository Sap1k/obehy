//! Two sources reporting one vehicle with nothing to bind them: the same journey, side by side
//! (BASE_PLAN.md section 27). Pure; the store writes it.
//!
//! When two reports from different sources run the same journey with fixes close in time and
//! place (`[pairing]` in the vehicle vocabulary), the evidence builds up; once confirmed they
//! are one vehicle (`bound_by = co_located`) while it holds. Confirmed pairs join transitively
//! ([`Pairings::cluster`]). They part when they are apart on that journey, when each runs
//! another journey, or when no close fix has come for `hold_s`.

use std::collections::{BTreeMap, BTreeSet};

use crate::geo::distance_m;
use crate::model::JourneyKey;
use crate::time::Instant;

/// A source's report of a vehicle: (source, source vehicle ID).
pub type Report = (String, String);
/// Two reports, the lesser first.
pub type Pair = (Report, Report);

/// When two sources' reports of one journey are one vehicle (`[pairing]`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Settings {
    pub distance_m: f64,
    pub apart_m: f64,
    pub max_fix_gap_s: f64,
    pub min_fixes: i64,
    pub confirm_s: f64,
    pub hold_s: f64,
}

/// One report's fix on a journey this tick.
#[derive(Debug, Clone, PartialEq)]
pub struct Fix {
    pub report: Report,
    pub journey: JourneyKey,
    pub lon: f64,
    pub lat: f64,
    pub at: Instant,
}

/// What two reports have shown together.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Evidence {
    pub first_seen: Instant,
    pub last_seen: Instant,
    pub fixes: i64,
    pub journey: JourneyKey,
    pub confirmed: bool,
}

/// The pairs of reports with evidence, and the changes not yet asked for.
///
/// Eviction: a pair leaves `pairs` when apart, on other journeys, or `hold_s` after its last
/// close fix; but that is checked only by `observe`, which runs on ticks with fixes, so pairs
/// stay while no source reports any vehicle on a road journey (a dead feed). `changed` is
/// drained by [`Pairings::changes`] on every write.
#[derive(Debug, Clone)]
pub struct Pairings {
    pub settings: Settings,
    pub pairs: BTreeMap<Pair, Evidence>,
    /// Pairs whose evidence changed since `changes()` was last asked (None: dropped).
    changed: BTreeMap<Pair, Option<Evidence>>,
}

impl Pairings {
    pub fn new(settings: Settings) -> Pairings {
        Pairings {
            settings,
            pairs: BTreeMap::new(),
            changed: BTreeMap::new(),
        }
    }

    /// Take this tick's fixes; return the reports whose pairing was confirmed or dropped.
    pub fn observe(&mut self, fixes: &[Fix], now: Instant) -> BTreeSet<Report> {
        let s = self.settings;
        let mut moved = BTreeSet::new();
        let mut journeys: BTreeMap<&Report, &JourneyKey> = BTreeMap::new();
        let mut by_journey: BTreeMap<&JourneyKey, Vec<&Fix>> = BTreeMap::new();
        for f in fixes {
            journeys.insert(&f.report, &f.journey);
            by_journey.entry(&f.journey).or_default().push(f);
        }
        for (journey, mut group) in by_journey {
            group.sort_by(|a, b| a.report.cmp(&b.report));
            for (i, a) in group.iter().enumerate() {
                for b in &group[i + 1..] {
                    if a.report.0 == b.report.0
                        || (a.at - b.at).total_seconds().abs() > s.max_fix_gap_s
                    {
                        continue;
                    }
                    let pair = (a.report.clone(), b.report.clone());
                    let gap = distance_m((a.lon, a.lat), (b.lon, b.lat));
                    if gap > s.apart_m {
                        moved.extend(self.drop(&pair));
                    } else if gap <= s.distance_m {
                        moved.extend(self.close(pair, journey, a.at.max(b.at)));
                    }
                }
            }
        }
        let parting: Vec<Pair> = self
            .pairs
            .iter()
            .filter(|(pair, evidence)| {
                let other_journeys = match (journeys.get(&pair.0), journeys.get(&pair.1)) {
                    (Some(ja), Some(jb)) => ja != jb,
                    _ => false,
                };
                other_journeys || (now - evidence.last_seen).total_seconds() > s.hold_s
            })
            .map(|(pair, _)| pair.clone())
            .collect();
        for pair in parting {
            moved.extend(self.drop(&pair));
        }
        moved
    }

    fn close(&mut self, pair: Pair, journey: &JourneyKey, at: Instant) -> Vec<Report> {
        let s = self.settings;
        let Some(evidence) = self.pairs.get_mut(&pair) else {
            let evidence = Evidence {
                first_seen: at,
                last_seen: at,
                fixes: 1,
                journey: journey.clone(),
                confirmed: false,
            };
            self.changed.insert(pair.clone(), Some(evidence.clone()));
            self.pairs.insert(pair, evidence);
            return Vec::new();
        };
        if at <= evidence.last_seen {
            return Vec::new(); // the same fixes again
        }
        evidence.last_seen = at;
        evidence.fixes += 1;
        evidence.journey = journey.clone();
        let sustained = (evidence.last_seen - evidence.first_seen).total_seconds() >= s.confirm_s;
        let confirm = !evidence.confirmed && evidence.fixes >= s.min_fixes && sustained;
        if confirm {
            evidence.confirmed = true;
        }
        let snapshot = evidence.clone();
        self.changed.insert(pair.clone(), Some(snapshot));
        if confirm {
            vec![pair.0, pair.1]
        } else {
            Vec::new()
        }
    }

    fn drop(&mut self, pair: &Pair) -> Vec<Report> {
        let Some(evidence) = self.pairs.remove(pair) else {
            return Vec::new();
        };
        self.changed.insert(pair.clone(), None);
        if evidence.confirmed {
            vec![pair.0.clone(), pair.1.clone()]
        } else {
            Vec::new()
        }
    }

    /// The reports confirmed to be the same vehicle as `report`, sorted.
    pub fn partners(&self, report: &Report) -> Vec<Report> {
        let mut out: Vec<Report> = self
            .pairs
            .iter()
            .filter(|((a, b), evidence)| evidence.confirmed && (a == report || b == report))
            .map(|((a, b), _)| if a == report { b.clone() } else { a.clone() })
            .collect();
        out.sort();
        out
    }

    /// The reports joined to `report` by confirmed pairs, directly or through others, `report`
    /// included; sorted.
    pub fn cluster(&self, report: &Report) -> Vec<Report> {
        let mut seen: BTreeSet<Report> = BTreeSet::from([report.clone()]);
        let mut frontier = vec![report.clone()];
        while let Some(next) = frontier.pop() {
            for partner in self.partners(&next) {
                if seen.insert(partner.clone()) {
                    frontier.push(partner);
                }
            }
        }
        seen.into_iter().collect()
    }

    /// The pairs changed since the last call (None: dropped), and forget them.
    pub fn changes(&mut self) -> BTreeMap<Pair, Option<Evidence>> {
        std::mem::take(&mut self.changed)
    }
}
