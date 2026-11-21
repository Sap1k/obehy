//! The crate graph is the import-linter contract (docs/RUST_PORT.md, "Purity, by crate"):
//! obehy-core uses no I/O crate, obehy-sources no async runtime, and neither depends on a crate
//! above it. Checked over the transitive dependencies Cargo.lock records (dev and build
//! dependencies included, so the check is if anything stricter than needed).

use std::collections::{BTreeMap, BTreeSet};

/// Crates that do I/O or networking: obehy-rt only.
const IO: &[&str] = &[
    "tokio",
    "async-std",
    "smol",
    "mio",
    "socket2",
    "hyper",
    "reqwest",
    "ureq",
    "h2",
    "rustls",
    "native-tls",
    "openssl",
    "postgres",
    "tokio-postgres",
    "postgres-types",
    "sqlx",
    "diesel",
    "libpq",
    "mimalloc",
    "jemallocator",
    "tikv-jemallocator",
    "notify",
    "walkdir",
];
/// Async runtimes and futures plumbing: not in obehy-sources either.
const ASYNC: &[&str] = &[
    "tokio",
    "async-std",
    "smol",
    "futures",
    "futures-util",
    "mio",
];

fn graph() -> BTreeMap<String, Vec<String>> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../Cargo.lock");
    let lock: toml::Table = std::fs::read_to_string(path)
        .expect("Cargo.lock")
        .parse()
        .expect("TOML");
    let mut graph = BTreeMap::new();
    for package in lock["package"].as_array().expect("packages") {
        let name = package["name"].as_str().unwrap().to_owned();
        let deps = package
            .get("dependencies")
            .and_then(|d| d.as_array())
            .map(|d| {
                d.iter()
                    .map(|x| x.as_str().unwrap().split(' ').next().unwrap().to_owned())
                    .collect()
            })
            .unwrap_or_default();
        graph
            .entry(name)
            .or_insert_with(Vec::new)
            .extend::<Vec<String>>(deps);
    }
    graph
}

fn closure(graph: &BTreeMap<String, Vec<String>>, root: &str) -> BTreeSet<String> {
    let mut seen = BTreeSet::new();
    let mut stack = vec![root.to_owned()];
    while let Some(name) = stack.pop() {
        for dep in graph.get(&name).into_iter().flatten() {
            if seen.insert(dep.clone()) {
                stack.push(dep.clone());
            }
        }
    }
    seen
}

#[test]
fn core_has_no_io_crate_and_no_crate_above_it() {
    let deps = closure(&graph(), "obehy-core");
    let bad: Vec<&String> = deps
        .iter()
        .filter(|d| IO.contains(&d.as_str()) || d.starts_with("obehy-"))
        .collect();
    assert!(bad.is_empty(), "obehy-core depends on {bad:?}");
}

#[test]
fn sources_have_no_io_or_async_crate_and_only_core_above() {
    let deps = closure(&graph(), "obehy-sources");
    let bad: Vec<&String> = deps
        .iter()
        .filter(|d| {
            IO.contains(&d.as_str())
                || ASYNC.contains(&d.as_str())
                || (d.starts_with("obehy-") && *d != "obehy-core")
        })
        .collect();
    assert!(bad.is_empty(), "obehy-sources depends on {bad:?}");
}
