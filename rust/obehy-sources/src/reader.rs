//! Format readers, bytes to values: JSON ([`json`], Python's `json`), HTML ([`html`], CPython's
//! `html.parser`) and GTFS-RT ([`gtfs_rt`], protobuf).

pub mod gtfs_rt;
pub mod html;
mod html_entities;
pub mod json;
