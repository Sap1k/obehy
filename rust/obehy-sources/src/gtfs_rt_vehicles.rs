//! The code decoder `gtfs-rt-vehicles`: a GTFS-RT vehicle positions dataset as observations,
//! one per vehicle entity, all on the manifest's one feed.
//!
//! Each observation names its vehicle (`VehicleDescriptor.id`), its trip (`trip_id` in the
//! manifest's trip namespace, and `start_date` as the service date when given) and, when the
//! fix is within the clock skew of the reception, its position at that time. A
//! `VehicleDescriptor.label` becomes the vehicle attribute the manifest names. The format's
//! delays live in trip updates; this decoder states none.

use obehy_core::model::{
    Fact, Feed, Observation, Position, RawRef, ServiceDay, TripKey, VehicleAttribute, VehicleKey,
};
use obehy_core::time::{Instant, within_skew};

use crate::error::PayloadError;
use crate::mapping::Context;
use crate::reader::gtfs_rt::{self, VehiclePosition};

/// How a channel's vehicle positions are read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VehicleFeed {
    /// The decoder version the observations carry.
    pub version: i64,
    pub feed: Feed,
    /// The namespace of the feed's trip IDs (`ids-jmk:gtfs_trip_id`).
    pub trip_namespace: String,
    /// The vehicle attribute a label is (`fleet_number`); labels are not read without one.
    pub label_attribute: Option<String>,
}

impl VehicleFeed {
    /// The dataset's observations in entity order; deleted entities and those without a
    /// vehicle ID are skipped.
    pub fn decode(
        &self,
        body: &[u8],
        sha256: &str,
        source: &str,
        channel: &str,
        ctx: &Context,
    ) -> Result<Vec<Observation>, PayloadError> {
        let message = gtfs_rt::parse(body)?;
        let mut out = Vec::new();
        for (item, entity) in message.entity.iter().enumerate() {
            let Some(vehicle) = &entity.vehicle else {
                continue;
            };
            if entity.is_deleted.unwrap_or(false) {
                continue;
            }
            let Some(facts) = self.facts(vehicle, ctx)? else {
                continue;
            };
            out.push(Observation {
                source: source.to_owned(),
                channel: channel.to_owned(),
                feed: self.feed,
                received_at: ctx.received_at,
                observed_at: facts.observed_at,
                raw: RawRef {
                    sha256: sha256.to_owned(),
                    item: item as i64,
                },
                decoder_version: self.version,
                facts: facts.facts,
            });
        }
        Ok(out)
    }

    fn facts(&self, v: &VehiclePosition, ctx: &Context) -> Result<Option<Facts>, PayloadError> {
        let descriptor = v.vehicle.as_ref();
        let Some(id) = descriptor.and_then(|d| present(d.id.as_ref())) else {
            return Ok(None);
        };
        let observed_at = self.observed_at(v, ctx)?;
        let mut facts = vec![Fact::VehicleKey(VehicleKey {
            source_vehicle_id: id,
        })];
        if let Some(trip) = &v.trip
            && let Some(trip_id) = present(trip.trip_id.as_ref())
        {
            facts.push(Fact::TripKey(TripKey {
                namespace: self.trip_namespace.clone(),
                key: trip_id,
            }));
            if let Some(start) = present(trip.start_date.as_ref()) {
                facts.push(Fact::ServiceDay(ServiceDay {
                    day: gtfs_rt::service_date(&start)?,
                }));
            }
        }
        if observed_at.is_some()
            && let Some(position) = v.position.as_ref().and_then(position)
        {
            facts.push(Fact::Position(position));
        }
        if let Some(attribute) = &self.label_attribute
            && let Some(label) = descriptor.and_then(|d| present(d.label.as_ref()))
        {
            facts.push(Fact::VehicleAttribute(VehicleAttribute {
                attribute: attribute.clone(),
                value: label,
            }));
        }
        Ok(Some(Facts { observed_at, facts }))
    }

    /// The fix time; none when absent (0) or beyond the clock skew of the reception.
    fn observed_at(
        &self,
        v: &VehiclePosition,
        ctx: &Context,
    ) -> Result<Option<Instant>, PayloadError> {
        let Some(epoch) = v.timestamp.filter(|t| *t != 0) else {
            return Ok(None);
        };
        let at = gtfs_rt::instant(epoch)?;
        Ok(within_skew(at, ctx.received_at, ctx.max_clock_skew).then_some(at))
    }
}

struct Facts {
    observed_at: Option<Instant>,
    facts: Vec<Fact>,
}

/// A position with both coordinates, neither 0 (the format's unset value).
fn position(p: &gtfs_rt::Position) -> Option<Position> {
    let (lat, lon) = (
        p.latitude.filter(|x| *x != 0.0)?,
        p.longitude.filter(|x| *x != 0.0)?,
    );
    Some(Position {
        lat: gtfs_rt::decimal(lat),
        lon: gtfs_rt::decimal(lon),
        bearing: p.bearing.map(gtfs_rt::decimal),
    })
}

fn present(text: Option<&String>) -> Option<String> {
    text.filter(|t| !t.is_empty()).cloned()
}
