//! Python semantics the port must reproduce exactly: numbers ([`num`]) and `json` ([`json`]).
//! See rust/docs/PORTING_HAZARDS.md for where each is needed.

pub mod json;
pub mod num;
