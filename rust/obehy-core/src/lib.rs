//! The realtime core (docs/RUST_PORT.md): time, geometry, facts and model types, policy and
//! manifests, and (later slices) the index, inference, the timeline, the core step, publishing
//! and the vehicle book. No I/O crate: bytes and files come from obehy-rt; the only file access
//! here is `config`'s convenience loaders.
//!
//! One file per Python module; modules of later slices are stubs naming what they port.

pub mod alerts;
pub mod compositions;
pub mod config;
pub mod core;
pub mod geo;
pub mod index;
pub mod infer;
pub mod model;
pub mod platforms;
pub mod publish;
pub mod py;
pub mod runs;
pub mod time;
pub mod timeline;
pub mod vehicles;
