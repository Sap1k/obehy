//! The realtime worker: the shell around the core (docs/RUST_PORT.md). The only crate that may
//! use tokio, a database driver or HTTP. So far the command line and the index's rows (R3).

pub mod archive;
pub mod canonical;
pub mod cli;
pub mod compare;
pub mod demand;
pub mod egress;
pub mod fetch;
pub mod index;
pub mod logs;
pub mod lookups;
pub mod phases;
pub mod record;
pub mod scheduler;
pub mod settings;
pub mod store;
pub mod warm;

// The shell drives the connectors (R2, R8).
use obehy_sources as _;
