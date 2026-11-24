//! Every channel's decoder, by (source, channel), built from the manifests' `[channel.decode]`:
//! One table, so a channel decodes the same in the
//! worker, in replay and in the compare harness.
//!
//! - [`Decoders::decode`] is the facts decoders' signature, `registry.DECODERS`: the body, its
//!   sha256, the reception time, the policy's clock skew and the poll's archived request (a
//!   board's station) → observations, or the payload's error.
//! - [`Decoders::decode_alerts`] is `registry.ALERT_DECODERS`: body and reception → alerts.
//! - Lookups (`cd/composition`) are [`crate::cd::plan`] and [`crate::cd::decode`].

use std::collections::HashMap;

use obehy_core::alerts::Alert;
use obehy_core::config::Source;
use obehy_core::config::decode::{CodeDecoder, Decode, GtfsRtKeys, OrChoice, Reader};
use obehy_core::model::Observation;
use obehy_core::time::{Instant, TimeDelta};

use crate::error::PayloadError;
use crate::gtfs_rt_alerts::AlertFeed;
use crate::gtfs_rt_vehicles::VehicleFeed;
use crate::mapping::{CompileError, Context, Files, Mapping};
use crate::reader::json::Value;
use crate::{pid_infotexts, sz_tabule};

/// How one channel decodes.
#[derive(Debug, Clone)]
pub enum Decoder {
    Mapping(Box<Mapping>),
    SzTabule { version: i64 },
    GtfsRtVehicles(Box<VehicleFeed>),
    GtfsRtAlerts(Box<AlertFeed>),
    PidInfotexts,
    CdComposition,
}

impl Decoder {
    /// `source` and `channel` name the channel the decode belongs to.
    pub fn compile(
        decode: &Decode,
        source: &str,
        channel: &str,
        files: Files<'_>,
    ) -> Result<Decoder, CompileError> {
        Ok(match decode.decoder {
            None => Decoder::Mapping(Box::new(Mapping::compile(decode, files)?)),
            Some(CodeDecoder::SzTabule) => Decoder::SzTabule {
                version: decode
                    .version
                    .ok_or_else(|| CompileError("sz-tabule needs `version`".into()))?,
            },
            Some(CodeDecoder::GtfsRtVehicles) => {
                Decoder::GtfsRtVehicles(Box::new(vehicle_feed(decode)?))
            }
            Some(CodeDecoder::GtfsRtAlerts) => Decoder::GtfsRtAlerts(Box::new(AlertFeed {
                source: source.to_owned(),
                channel: channel.to_owned(),
                keys: gtfs_rt(decode, "gtfs-rt-alerts")?.clone(),
            })),
            Some(CodeDecoder::PidInfotexts) => Decoder::PidInfotexts,
            Some(CodeDecoder::CdComposition) => Decoder::CdComposition,
        })
    }

    /// The decoder version its observations carry; None for alerts and lookups.
    pub fn version(&self) -> Option<i64> {
        match self {
            Decoder::Mapping(m) => Some(m.version),
            Decoder::SzTabule { version } => Some(*version),
            Decoder::GtfsRtVehicles(v) => Some(v.version),
            _ => None,
        }
    }
}

/// A GTFS-RT decoder's keys; its reader must be `gtfs-rt`.
fn gtfs_rt<'d>(decode: &'d Decode, decoder: &str) -> Result<&'d GtfsRtKeys, CompileError> {
    if decode.reader != Reader::GtfsRt {
        return Err(CompileError(format!(
            "{decoder} needs `reader = \"gtfs-rt\"`"
        )));
    }
    decode
        .keys
        .as_ref()
        .ok_or_else(|| CompileError(format!("{decoder} needs `keys`")))
}

fn vehicle_feed(decode: &Decode) -> Result<VehicleFeed, CompileError> {
    let need = |what: &str| CompileError(format!("gtfs-rt-vehicles needs {what}"));
    let keys = gtfs_rt(decode, "gtfs-rt-vehicles")?;
    let Some(OrChoice::Plain(feed)) = decode.feed else {
        return Err(need("one `feed`"));
    };
    Ok(VehicleFeed {
        version: decode.version.ok_or_else(|| need("`version`"))?,
        feed: feed.into(),
        trip_namespace: keys.trip.clone().ok_or_else(|| need("`keys.trip`"))?,
        label_attribute: decode.label_attribute.clone(),
    })
}

/// The decoders of every channel and lookup of the manifests.
#[derive(Debug, Clone, Default)]
pub struct Decoders {
    by_channel: HashMap<(String, String), Decoder>,
}

impl Decoders {
    /// Compiles every `[channel.decode]` and `[lookup.decode]`; `files` reads what a manifest
    /// names (relative to the Python package directory, `src/obehy`).
    pub fn new(sources: &[Source], files: Files<'_>) -> Result<Decoders, CompileError> {
        let mut by_channel = HashMap::new();
        for source in sources {
            let channels = source
                .channels
                .iter()
                .chain(source.lookups.iter().map(|l| &l.request));
            for channel in channels {
                let Some(decode) = &channel.decode else {
                    continue;
                };
                let decoder = Decoder::compile(decode, &channel.source, &channel.channel, files)
                    .map_err(|e| CompileError(format!("{}: {e}", channel.name())))?;
                by_channel.insert((channel.source.clone(), channel.channel.clone()), decoder);
            }
        }
        Ok(Decoders { by_channel })
    }

    pub fn get(&self, source: &str, channel: &str) -> Option<&Decoder> {
        self.by_channel
            .get(&(source.to_owned(), channel.to_owned()))
    }

    /// Whether the channel decodes to facts (not alerts, not a lookup).
    pub fn decodes_facts(&self, source: &str, channel: &str) -> bool {
        matches!(
            self.get(source, channel),
            Some(Decoder::Mapping(_) | Decoder::SzTabule { .. } | Decoder::GtfsRtVehicles(_))
        )
    }

    pub fn is_alert_channel(&self, source: &str, channel: &str) -> bool {
        matches!(
            self.get(source, channel),
            Some(Decoder::GtfsRtAlerts(_) | Decoder::PidInfotexts)
        )
    }

    /// The decoder version of a facts channel (`DECODER_VERSIONS`).
    pub fn version(&self, source: &str, channel: &str) -> Option<i64> {
        self.get(source, channel).and_then(Decoder::version)
    }

    /// A poll's observations (`DECODERS[(source, channel)](body, sha256, received_at, skew,
    /// request)`).
    #[allow(clippy::too_many_arguments)]
    pub fn decode(
        &self,
        source: &str,
        channel: &str,
        body: &[u8],
        sha256: &str,
        received_at: Instant,
        max_clock_skew: TimeDelta,
        request: Option<&Value>,
    ) -> Result<Vec<Observation>, PayloadError> {
        let ctx = Context {
            received_at,
            max_clock_skew,
        };
        match self.get(source, channel) {
            Some(Decoder::Mapping(m)) => m.decode(body, sha256, source, channel, &ctx),
            Some(Decoder::GtfsRtVehicles(v)) => v.decode(body, sha256, source, channel, &ctx),
            Some(Decoder::SzTabule { version }) => {
                sz_tabule::decode(body, sha256, received_at, request, *version)
            }
            _ => Err(PayloadError::new(format!(
                "no realtime connector decodes {source}/{channel} to facts"
            ))),
        }
    }

    /// An alert channel's alerts (`ALERT_DECODERS`).
    pub fn decode_alerts(
        &self,
        source: &str,
        channel: &str,
        body: &[u8],
        received_at: Instant,
    ) -> Result<Vec<Alert>, PayloadError> {
        match self.get(source, channel) {
            Some(Decoder::GtfsRtAlerts(a)) => a.decode(body),
            Some(Decoder::PidInfotexts) => pid_infotexts::decode_infotexts(body, received_at),
            _ => Err(PayloadError::new(format!(
                "{source}/{channel} is no alert channel"
            ))),
        }
    }
}
