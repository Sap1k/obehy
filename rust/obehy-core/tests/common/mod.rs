//! Fixture access for the Python parity tests (`rust/tools/gen_fixtures.py` writes them).
#![allow(dead_code)]

use std::path::PathBuf;

pub fn path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

pub fn text(name: &str) -> String {
    std::fs::read_to_string(path(name)).unwrap_or_else(|e| panic!("{name}: {e}"))
}

pub fn json(name: &str) -> serde_json::Value {
    serde_json::from_str(&text(name)).unwrap_or_else(|e| panic!("{name}: {e}"))
}

/// A float from its IEEE bits in hex.
pub fn float(value: &serde_json::Value) -> f64 {
    bits(value.as_str().expect("hex bits"))
}

pub fn bits(hex: &str) -> f64 {
    f64::from_bits(u64::from_str_radix(hex, 16).expect("hex bits"))
}

pub fn int(value: &serde_json::Value) -> i64 {
    value.as_i64().expect("an integer")
}

/// The repository root (the Python package lives there).
pub fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
}
