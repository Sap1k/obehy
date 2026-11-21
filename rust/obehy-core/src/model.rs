//! Facts, observations and the core's model types, and the facts' JSON: ports of
//! `realtime/facts.py` ([`facts`]), `realtime/model.py` ([`types`]) and
//! `realtime/model_json.py` ([`json`]); [`state_json`] reads the `FeedState` JSON of the
//! Python fixture generators. Everything is re-exported here, as `model.py`
//! re-exports `facts.py`.

pub mod facts;
pub mod json;
pub mod state_json;
pub mod types;

pub use facts::*;
pub use types::*;
