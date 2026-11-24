//! Connectors (docs/RUST_PORT.md, "Config-driven sources"): payload bytes in, facts out. No
//! I/O and no async: polling, egress and archiving are obehy-rt's.
//!
//! A channel's manifest says how its payload is decoded (`[channel.decode]`,
//! `obehy_core::config::decode`): most channels by the declarative [`mapping`] with the named
//! normalizers of [`normalize`], the rest by a code decoder ([`sz_tabule`],
//! [`gtfs_rt_alerts`], [`gtfs_rt_vehicles`], [`pid_infotexts`], [`cd`]).
//! [`registry::Decoders`] builds them all from the manifests.

pub mod arriva_express;
pub mod cd;
pub mod dpmd;
pub mod error;
pub mod gtfs_rt_alerts;
pub mod gtfs_rt_vehicles;
pub mod krovak;
pub mod mapping;
pub mod normalize;
pub mod pid_infotexts;
pub mod reader;
pub mod registry;
pub mod sz_tabule;
pub mod value;

pub use error::PayloadError;
pub use registry::{Decoder, Decoders};
