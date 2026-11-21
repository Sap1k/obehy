//! The periodic summary's memory line (docs/RUST_PORT.md "Memory"): resident memory and the size
//! of every long-lived container, so growth shows in the log before it shows in swap.

use obehy_core::runner::Loads;

use super::pipeline::Pipeline;
use crate::warm::rss_bytes;

fn megabytes(bytes: Option<u64>) -> String {
    bytes.map_or_else(
        || "?".to_owned(),
        |b| format!("{:.1} MB", b as f64 / 1_048_576.0),
    )
}

/// `memory: rss …; <feed>: index …, plans …, state …; book …; emitters …; alerts …`.
pub fn sizes_line<L: Loads>(pipeline: &Pipeline<L>) -> String {
    let mut parts = vec![format!("rss {}", megabytes(rss_bytes()))];
    for feed in pipeline.runner.sorted_feeds() {
        let runtime = &pipeline.runner.runtimes[&feed];
        let loader = &runtime.loader;
        let index = &loader.index;
        let (paths, plans) = runtime.plans.sizes();
        let state = &runtime.state;
        let traversals: usize = state.hops.values().map(|h| h.len()).sum();
        parts.push(format!(
            "{}: index keys {} trips {} services {} dates {}/{} locations {} shapes {} runs {} \
             lines {} ids {} used {}/{}, plans {paths}/{plans}, state vehicles {} instances {} \
             dirty {} hops {}/{traversals}",
            feed.as_str(),
            index.key_entries.len(),
            index.trips.len(),
            index.trip_services.len(),
            index.service_dates.len(),
            index.loaded_dates.len(),
            index.locations.len(),
            index.shapes.len(),
            index.runs.len(),
            index.lines.len(),
            index.pool.len(),
            loader.used.len(),
            loader.lines_used.len(),
            state.vehicles.len(),
            state.instances.len(),
            state.dirty.len(),
            state.hops.len(),
        ));
    }
    let output = &pipeline.output;
    if let Some(writer) = &output.writer {
        let (live, identities, members) = writer.vehicles.sizes();
        parts.push(format!(
            "book live {live} identities {identities} members {members}"
        ));
    }
    let emitters: Vec<String> = output
        .emitters
        .iter()
        .map(|(feed, e)| format!("{} {}", feed.as_str(), e.cached()))
        .collect();
    parts.push(format!("emitters {}", emitters.join(" ")));
    if let Some(alerts) = &output.alerts {
        parts.push(format!("alerts {}", alerts.cached()));
    }
    format!("memory: {}", parts.join("; "))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resident_memory_is_measured() {
        let rss = rss_bytes().expect("resident memory on this platform");
        assert!(rss > 1_000_000, "{rss}");
        assert_eq!(megabytes(Some(1_572_864)), "1.5 MB");
    }
}
