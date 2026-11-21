//! Time per pipeline phase (archive, decode, core, writes, emit) for the worker's periodic summary
//! and `obehy-rt bench`.
//!
//! Each phase gets thread CPU time and wall time: the CPU is what the calling thread spent (its
//! fetches run elsewhere), the wall time also counts waiting on the database.

use std::collections::BTreeMap;
use std::time::Instant as Monotonic;

use cpu_time::ThreadTime;
use indexmap::IndexMap;

#[derive(Debug, Default, Clone)]
pub struct Phases {
    /// In first-timed order.
    pub cpu_s: IndexMap<String, f64>,
    pub wall_s: IndexMap<String, f64>,
    pub calls: BTreeMap<String, u64>,
    /// Items processed, such as observations.
    pub counts: BTreeMap<String, u64>,
}

impl Phases {
    pub fn new() -> Phases {
        Phases::default()
    }

    pub fn count(&mut self, name: &str, n: u64) {
        *self.counts.entry(name.to_owned()).or_default() += n;
    }

    /// Run `f`, adding its thread CPU and wall time to `name`.
    pub fn time<T>(&mut self, name: &str, f: impl FnOnce() -> T) -> T {
        let cpu = ThreadTime::try_now().ok();
        let wall = Monotonic::now();
        let result = f();
        self.add(
            name,
            cpu.map_or(0.0, |c| c.elapsed().as_secs_f64()),
            wall.elapsed().as_secs_f64(),
        );
        result
    }

    /// Add measured time to a phase (for phases timed across an await).
    pub fn add(&mut self, name: &str, cpu_s: f64, wall_s: f64) {
        *self.cpu_s.entry(name.to_owned()).or_default() += cpu_s;
        *self.wall_s.entry(name.to_owned()).or_default() += wall_s;
        *self.calls.entry(name.to_owned()).or_default() += 1;
    }

    /// Each phase's CPU and wall time as a share of one core over `over_s` seconds.
    pub fn line(&self, over_s: f64) -> String {
        let cpu: f64 = self.cpu_s.values().sum();
        let wall: f64 = self.wall_s.values().sum();
        let mut phases: Vec<(&String, f64)> = self.wall_s.iter().map(|(k, v)| (k, *v)).collect();
        // `sorted(key=-wall)`: stable, ties keep first-timed order.
        phases.sort_by(|a, b| b.1.total_cmp(&a.1));
        let parts: Vec<String> = phases
            .iter()
            .map(|(name, spent)| {
                format!(
                    "{name} {:.1}/{:.1}%",
                    100.0 * self.cpu_s[*name] / over_s,
                    100.0 * spent / over_s
                )
            })
            .collect();
        let observations = self.counts.get("observations").copied().unwrap_or(0);
        let per = match self.cpu_s.get("core") {
            Some(core) if observations > 0 => format!(
                "; core {:.0} µs per observation",
                1e6 * core / observations as f64
            ),
            _ => String::new(),
        };
        format!(
            "cpu/wall {:.1}/{:.1}% of a core: {}{per}",
            100.0 * cpu / over_s,
            100.0 * wall / over_s,
            parts.join(", ")
        )
    }

    pub fn reset(&mut self) {
        self.cpu_s.clear();
        self.wall_s.clear();
        self.calls.clear();
        self.counts.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_line_lists_phases_by_wall_time_with_core_per_observation() {
        let mut phases = Phases::new();
        phases.add("decode", 0.5, 1.0);
        phases.add("core", 2.0, 2.0);
        phases.count("observations", 1000);
        assert_eq!(
            phases.line(10.0),
            "cpu/wall 25.0/30.0% of a core: core 20.0/20.0%, decode 5.0/10.0%; \
             core 2000 µs per observation"
        );
        let value = phases.time("archive", || 7);
        assert_eq!((value, phases.calls["archive"]), (7, 1));
        phases.reset();
        assert!(phases.wall_s.is_empty() && phases.counts.is_empty());
    }
}
