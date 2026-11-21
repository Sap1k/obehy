//! Connectors (docs/RUST_PORT.md, "Config-driven sources"): payload bytes in, facts out. No
//! I/O and no async: polling, egress and archiving are obehy-rt's. Filled in R2.

pub mod arriva_express;
pub mod cd;
pub mod decoding;
pub mod dpmd;
pub mod duk;
pub mod mapping;
pub mod mpvnet;
pub mod normalize;
pub mod pid;
pub mod pid_alerts;
pub mod reader;
pub mod registry;
pub mod sz;
pub mod sz_tabule;

// The connectors build facts from obehy-core types (R2).
use obehy_core as _;
