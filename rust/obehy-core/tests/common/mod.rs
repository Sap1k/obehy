//! Shared test helpers: the timetable builder, the corpus trace reader and the repository root.
#![allow(dead_code)]

use std::path::PathBuf;

pub mod builder;
pub mod trace;

/// The repository root (the Python package lives there).
pub fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
}
