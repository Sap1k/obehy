//! The vehicle book (R7): ports of `realtime/emit/vehicles.py` ([`book`]) and
//! `emit/pairing.py` ([`pairing`]), with the parts of `reference/` the book decides with: the
//! vocabulary ([`vocabulary`]), the registry in memory ([`registry`]) and attribute precedence
//! ([`precedence`]).

pub mod book;
pub mod pairing;
pub mod precedence;
pub mod registry;
pub mod vocabulary;

pub use book::{AttributeRow, Identity, Sighting, VehicleBook, VehicleRows};
pub use pairing::{Evidence, Fix, Pair, Pairings, Report};
pub use registry::{BoundBy, Registry};
pub use vocabulary::Vocabulary;
