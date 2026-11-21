//! Policy ([`policy`]) and connector manifests ([`manifest`]).
//!
//! The files live in the Python package (`src/obehy/data/realtime/policy-v18.toml`,
//! `src/obehy/data/realtime/sources/*.toml`), which reads a few of their fields too (tomllib,
//! TOML 1.0: keep the files to TOML 1.0); obehy-rt finds them, this module parses them.

pub mod decode;
pub mod manifest;
pub mod policy;

pub use manifest::{Channel, Lookup, Source};
pub use policy::{ByMode, Policy};

/// A policy or manifest file that does not load.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ConfigError {
    #[error("policy: {0}")]
    Policy(String),
    #[error("{0}")]
    Manifest(String),
}
