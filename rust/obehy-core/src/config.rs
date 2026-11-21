//! Policy and connector manifests, read from the same TOML files as Python: ports of
//! `realtime/policy.py` ([`policy`]) and `realtime/manifest.py` ([`manifest`]).
//!
//! The files live in the Python package (`src/obehy/data/realtime/policy-v18.toml`,
//! `src/obehy/realtime/sources/*.toml`); obehy-rt finds them, this module parses them. TOML
//! itself: the `toml` crate reads TOML 1.1, tomllib 1.0; a file valid in 1.0 reads the same in
//! both, but one using 1.1-only syntax would load here and fail in Python.

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
