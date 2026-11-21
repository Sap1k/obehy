//! The realtime worker: the shell around the core (docs/RUST_PORT.md). The only crate that may
//! use tokio, a database driver or HTTP. For now the command line only.

pub mod cli;

// The shell drives the connectors (R2, R8).
use obehy_sources as _;
