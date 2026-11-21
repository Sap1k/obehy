//! The realtime worker: the shell around the core (docs/RUST_PORT.md). The only crate that may
//! use tokio, a database driver or HTTP. So far the command line and the index's rows (R3).

pub mod archive;
pub mod bench;
pub mod canonical;
pub mod cli;
pub mod compare;
pub mod corpus;
pub mod demand;
pub mod egress;
pub mod fetch;
pub mod gtfs_json;
pub mod history;
pub mod index;
pub mod logs;
pub mod lookups;
pub mod output;
pub mod phases;
pub mod plain;
pub mod record;
pub mod replay;
pub mod samples;
pub mod scheduler;
pub mod settings;
pub mod store;
pub mod warm;
pub mod worker;

/// The global allocator `obehy-rt` is built with (`bench.json` names it; the soak chose it).
pub const ALLOCATOR: &str = if cfg!(feature = "mimalloc") {
    "mimalloc"
} else {
    "system"
};

// The shell drives the connectors (R2, R8).
use obehy_sources as _;
