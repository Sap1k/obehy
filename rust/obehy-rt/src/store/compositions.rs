//! Writing compositions (BASE_PLAN.md section 28): port of `emit/compositions.py`. A source's
//! answer about a journey's formation, merged with the journey's earlier answers. Written by
//! the lookup broker (`crate::lookups`); read by the API through `rt.composition_spans`.

use std::collections::{BTreeMap, HashMap};

use obehy_core::compositions::{TripView, Unit, ViewCall};
use obehy_core::config::policy::JourneyPolicy;
use obehy_core::py::json::{DumpOptions, PyJson, dumps};
use obehy_core::time::{Date, Instant};
use sha2::{Digest, Sha256};
use tokio_postgres::Client;

use super::StoreResult;
use super::cells::{Jsonb, PgTime};
use crate::index::sql::PgDate;

fn pairs_json(pairs: &[(String, String)]) -> PyJson {
    // `dict(pairs)`: the last of equal keys wins, at the first one's position.
    let mut out: Vec<(String, PyJson)> = Vec::new();
    for (k, v) in pairs {
        match out.iter_mut().find(|(key, _)| key == k) {
            Some(slot) => slot.1 = PyJson::str(v),
            None => out.push((k.clone(), PyJson::str(v))),
        }
    }
    PyJson::from_pairs(out)
}

fn unit_row(unit: &Unit) -> PyJson {
    PyJson::object([
        ("sequence", PyJson::Int(unit.sequence)),
        ("kind", PyJson::str(&unit.kind)),
        ("role", PyJson::str(&unit.role)),
        (
            "vehicle_type",
            PyJson::opt(unit.vehicle_type.as_deref(), PyJson::str),
        ),
        (
            "coach_number",
            PyJson::opt(unit.coach_number.as_deref(), PyJson::str),
        ),
        (
            "classes",
            PyJson::List(unit.classes.iter().map(|c| PyJson::Int(*c)).collect()),
        ),
        (
            "features",
            PyJson::List(unit.features.iter().map(PyJson::str).collect()),
        ),
        ("texts", pairs_json(&unit.texts)),
        (
            "destination",
            PyJson::opt(unit.destination.as_deref(), PyJson::str),
        ),
        ("details", pairs_json(&unit.details)),
    ])
}

/// Tells identical answers apart from changed ones: sha256 of the units' sorted, compact,
/// UTF-8 JSON (`digest`).
pub fn digest(units: &[Unit]) -> String {
    let rows = PyJson::List(units.iter().map(unit_row).collect());
    let text = dumps(&rows, DumpOptions::COMPACT.utf8().sorted());
    let hash = Sha256::digest(text.as_bytes());
    hash.iter().map(|b| format!("{b:02x}")).collect()
}

/// The journey a composition is kept under: the trip's journey key (policy `[journey]`), else
/// its trip ID.
pub fn journey_of(journey: &JourneyPolicy, view: &TripView) -> (String, String, String, Date) {
    let keys: BTreeMap<String, String> = view
        .keys
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let (namespace, key) = match journey.key_of(&keys) {
        Some((namespace, key)) => (namespace.to_owned(), key.to_owned()),
        None => ("trip_id".to_owned(), view.trip_id.clone()),
    };
    (view.feed.clone(), namespace, key, view.service_date)
}

async fn write_units(client: &Client, composition_id: i64, units: &[Unit]) -> StoreResult<()> {
    client
        .execute(
            "DELETE FROM rt.composition_unit WHERE composition_id = $1",
            &[&composition_id],
        )
        .await?;
    let insert = client
        .prepare(
            "INSERT INTO rt.composition_unit (composition_id, sequence, kind, role, \
             vehicle_type, coach_number, classes, features, texts, destination, details) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        )
        .await?;
    for u in units {
        let sequence =
            i32::try_from(u.sequence).map_err(|e| super::StoreError::other(e.to_string()))?;
        let classes: Vec<i32> = u
            .classes
            .iter()
            .map(|c| i32::try_from(*c).unwrap_or(i32::MAX))
            .collect();
        let texts = Jsonb(dumps(&pairs_json(&u.texts), DumpOptions::DEFAULT));
        let details = Jsonb(dumps(&pairs_json(&u.details), DumpOptions::DEFAULT));
        client
            .execute(
                &insert,
                &[
                    &composition_id,
                    &sequence,
                    &u.kind,
                    &u.role,
                    &u.vehicle_type,
                    &u.coach_number,
                    &classes,
                    &u.features,
                    &texts,
                    &u.destination,
                    &details,
                ],
            )
            .await?;
    }
    Ok(())
}

struct Stored {
    composition_id: i64,
    from: (String, i32),
    to: Option<(String, i32)>,
    digest: String,
}

/// Store an answer holding from `start` onward, merged with the journey's earlier answers of
/// the source (`write_composition`, BASE_PLAN.md section 28): the same start replaces its
/// units; an identical answer already holding there is extended; otherwise the answer holds
/// until the next later start, and an earlier answer still holding at `start` ends there.
/// Returns the composition's ID.
#[allow(clippy::too_many_arguments)]
pub async fn write_composition(
    client: &Client,
    journey: &JourneyPolicy,
    view: &TripView,
    source: &str,
    start: &ViewCall,
    units: &[Unit],
    at: Instant,
    fetch_id: i64,
) -> StoreResult<i64> {
    let (feed, namespace, key, service_date) = journey_of(journey, view);
    let order: HashMap<(&str, i64), i64> = view
        .calls
        .iter()
        .map(|c| ((c.location_id.as_str(), c.visit_n), c.ordinal))
        .collect();
    let new_digest = digest(units);
    let ordinal =
        |place: &(String, i32)| order.get(&(place.0.as_str(), i64::from(place.1))).copied();
    client.batch_execute("BEGIN").await?;
    let outcome = async {
        let day = PgDate(service_date);
        let rows = client
            .query(
                "SELECT composition_id, from_location, from_visit, to_location, to_visit, \
                 digest FROM rt.composition WHERE feed = $1 AND key_namespace = $2 AND key = $3 \
                 AND service_date = $4 AND source = $5 FOR UPDATE",
                &[&feed, &namespace, &key, &day, &source],
            )
            .await?;
        let mut placed: Vec<(i64, Stored)> = Vec::new();
        for r in rows {
            let to_location: Option<String> = r.try_get(3)?;
            let to_visit: Option<i32> = r.try_get(4)?;
            let stored = Stored {
                composition_id: r.try_get(0)?,
                from: (r.try_get(1)?, r.try_get(2)?),
                to: to_location.map(|l| (l, to_visit.unwrap_or_default())),
                digest: r.try_get(5)?,
            };
            if let Some(o) = ordinal(&stored.from) {
                placed.push((o, stored));
            }
        }
        // Python sorts (ordinal, row) tuples; two rows at one ordinal would compare dict rows
        // (an error there), so ordinals are unique in practice: sort by ordinal.
        placed.sort_by_key(|(o, _)| *o);
        let touch = "UPDATE rt.composition SET observed_at = $1, fetch_id = $2, digest = $3 \
                     WHERE composition_id = $4";
        if let Some((_, same)) = placed.iter().find(|(o, _)| *o == start.ordinal) {
            client
                .execute(
                    touch,
                    &[&PgTime(at), &fetch_id, &new_digest, &same.composition_id],
                )
                .await?;
            if same.digest != new_digest {
                write_units(client, same.composition_id, units).await?;
            }
            return Ok(same.composition_id);
        }
        let previous = placed.iter().rev().find(|(o, _)| *o < start.ordinal);
        let following = placed.iter().find(|(o, _)| *o > start.ordinal);
        if let Some((_, previous)) = previous {
            let until = previous.to.as_ref().and_then(ordinal);
            // A `to` that is not one of the trip's calls reads as None: still holding.
            let holding = until.is_none_or(|u| u > start.ordinal);
            if previous.digest == new_digest && holding {
                client
                    .execute(
                        touch,
                        &[
                            &PgTime(at),
                            &fetch_id,
                            &new_digest,
                            &previous.composition_id,
                        ],
                    )
                    .await?;
                return Ok(previous.composition_id);
            }
            if holding {
                let visit = i32::try_from(start.visit_n).unwrap_or(i32::MAX);
                client
                    .execute(
                        "UPDATE rt.composition SET to_location = $1, to_visit = $2 \
                         WHERE composition_id = $3",
                        &[&start.location_id, &visit, &previous.composition_id],
                    )
                    .await?;
            }
        }
        let visit = i32::try_from(start.visit_n).unwrap_or(i32::MAX);
        let (to_location, to_visit) = match following {
            Some((_, f)) => (Some(f.from.0.clone()), Some(f.from.1)),
            None => (None, None),
        };
        let created = client
            .query_one(
                "INSERT INTO rt.composition (feed, key_namespace, key, service_date, source, \
                 from_location, from_visit, to_location, to_visit, digest, first_seen, \
                 observed_at, fetch_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, \
                 $11, $12) RETURNING composition_id",
                &[
                    &feed,
                    &namespace,
                    &key,
                    &day,
                    &source,
                    &start.location_id,
                    &visit,
                    &to_location,
                    &to_visit,
                    &new_digest,
                    &PgTime(at),
                    &fetch_id,
                ],
            )
            .await?;
        let composition_id: i64 = created.try_get(0)?;
        write_units(client, composition_id, units).await?;
        Ok(composition_id)
    }
    .await;
    match outcome {
        Ok(id) => {
            client.batch_execute("COMMIT").await?;
            Ok(id)
        }
        Err(error) => {
            let _ = client.batch_execute("ROLLBACK").await;
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unit(sequence: i64) -> Unit {
        Unit {
            sequence,
            kind: "coach".into(),
            role: "train".into(),
            vehicle_type: Some("Bmz".into()),
            coach_number: None,
            classes: vec![2],
            features: vec!["wifi".into()],
            texts: vec![("a".into(), "Klimatizace".into())],
            destination: Some("Praha hl.n.".into()),
            details: Vec::new(),
        }
    }

    #[test]
    fn the_digest_is_pythons_text_hashed() {
        // sha256 of `[{"classes":[2],"coach_number":null,"destination":"Praha hl.n.",
        // "details":{},"features":["wifi"],"kind":"coach","role":"train","sequence":1,
        // "texts":{"a":"Klimatizace"},"vehicle_type":"Bmz"}]` (Python's json.dumps).
        let text = "[{\"classes\":[2],\"coach_number\":null,\"destination\":\"Praha hl.n.\",\
                    \"details\":{},\"features\":[\"wifi\"],\"kind\":\"coach\",\"role\":\"train\",\
                    \"sequence\":1,\"texts\":{\"a\":\"Klimatizace\"},\"vehicle_type\":\"Bmz\"}]";
        let expected: String = Sha256::digest(text.as_bytes())
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        assert_eq!(digest(&[unit(1)]), expected);
        assert_ne!(digest(&[unit(1)]), digest(&[unit(2)]));
    }
}
