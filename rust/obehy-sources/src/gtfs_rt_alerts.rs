//! The code decoder `gtfs-rt-alerts`: a GTFS-RT service alerts dataset as alerts. The feed's
//! trip, stop and route IDs are read in the namespaces the manifest names (`keys`).

use obehy_core::alerts::{Alert, AlertEntity, Period, Texts};
use obehy_core::config::decode::GtfsRtKeys;

use crate::error::PayloadError;
use crate::reader::gtfs_rt::{
    self, EntitySelector, TranslatedString, cause_name, effect_name, severity_name,
};

/// What a channel's alerts are: whose they are, and its keys.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertFeed {
    pub source: String,
    pub channel: String,
    pub keys: GtfsRtKeys,
}

impl AlertFeed {
    /// The dataset's alerts sorted by ID; entities without an alert, and deleted ones, are
    /// skipped.
    pub fn decode(&self, body: &[u8]) -> Result<Vec<Alert>, PayloadError> {
        let message = gtfs_rt::parse(body)?;
        let mut alerts = Vec::new();
        for entity in &message.entity {
            let Some(a) = &entity.alert else {
                continue;
            };
            if entity.is_deleted.unwrap_or(false) {
                continue;
            }
            let mut entities = Vec::new();
            for informed in &a.informed_entity {
                if let Some(found) = self.informed(informed)? {
                    entities.push(found);
                }
            }
            entities.sort_by(|a, b| {
                (&a.kind, &a.identifier, a.service_date).cmp(&(
                    &b.kind,
                    &b.identifier,
                    b.service_date,
                ))
            });
            entities.dedup();
            let periods = a
                .active_period
                .iter()
                .map(|p| {
                    Ok(Period {
                        start: p.start.map(gtfs_rt::instant).transpose()?,
                        end: p.end.map(gtfs_rt::instant).transpose()?,
                    })
                })
                .collect::<Result<_, PayloadError>>()?;
            alerts.push(Alert {
                source: self.source.clone(),
                channel: self.channel.clone(),
                alert_id: entity.id.clone().unwrap_or_default(),
                cause: cause_name(a.cause).into(),
                effect: effect_name(a.effect).into(),
                severity: severity_name(a.severity_level).into(),
                periods,
                header: translated(a.header_text.as_ref()),
                description: translated(a.description_text.as_ref()),
                url: translated(a.url.as_ref()),
                cause_detail: translated(a.cause_detail.as_ref()),
                effect_detail: translated(a.effect_detail.as_ref()),
                entities,
                details: Vec::new(),
            });
        }
        alerts.sort_by(|a, b| a.alert_id.cmp(&b.alert_id));
        Ok(alerts)
    }

    /// What a selector names, most specific first: a trip (with its start date as the service
    /// date), else a stop, else a route; none if that ID is empty or its kind has no namespace.
    fn informed(&self, informed: &EntitySelector) -> Result<Option<AlertEntity>, PayloadError> {
        let present = |id: Option<&String>| id.filter(|s| !s.is_empty()).cloned();
        let entity = |kind: &str, namespace: &Option<String>, identifier, service_date| {
            namespace.as_ref().map(|ns| AlertEntity {
                kind: kind.into(),
                namespace: ns.clone(),
                identifier,
                service_date,
            })
        };
        if let Some(trip) = &informed.trip
            && let Some(id) = present(trip.trip_id.as_ref())
        {
            let day = present(trip.start_date.as_ref())
                .map(|d| gtfs_rt::service_date(&d))
                .transpose()?;
            return Ok(entity("trip", &self.keys.trip, id, day));
        }
        if let Some(id) = present(informed.stop_id.as_ref()) {
            return Ok(entity("stop", &self.keys.stop, id, None));
        }
        Ok(present(informed.route_id.as_ref())
            .and_then(|id| entity("route", &self.keys.route, id, None)))
    }
}

/// A translated text as sorted `(language, text)` pairs; empty texts are dropped and a missing
/// language is Czech.
fn translated(value: Option<&TranslatedString>) -> Texts {
    let mut found: Texts = value
        .map(|v| v.translation.as_slice())
        .unwrap_or_default()
        .iter()
        .filter_map(|t| {
            let text = t.text.clone().filter(|t| !t.is_empty())?;
            let lang = t
                .language
                .clone()
                .filter(|l| !l.is_empty())
                .unwrap_or_else(|| "cs".into());
            Some((lang, text))
        })
        .collect();
    found.sort();
    found
}
