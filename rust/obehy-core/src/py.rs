//! Python semantics the port must reproduce exactly: numbers ([`num`]), `json` ([`json`]) and
//! `str` ([`text`], with Python's Unicode tables). See rust/docs/PORTING_HAZARDS.md for where
//! each is needed.

pub mod json;
pub mod num;
pub mod text;
mod unicode;
