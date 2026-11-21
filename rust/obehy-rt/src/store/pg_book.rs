//! The vehicle book's SQL (`emit/vehicles.py`'s `VehicleBook.load`, `write_vehicle_rows`,
//! `write_physical_policy`, and `reference/registry.py`'s `load_registry`), queries text for
//! text. Row-by-row statements are pipelined: sent together, executed in order, as psycopg's
//! `executemany` does.

use futures_util::future::try_join_all;
use obehy_core::model::{Feed, JourneyKey};
use obehy_core::py::json::{DumpOptions, PyJson, dumps};
use obehy_core::time::Date;
use tokio_postgres::Client;
use tokio_postgres::types::ToSql;

use super::cells::{JsonText, Jsonb, PgTime};
use super::vehicles::{
    CuratedValueRow, EntryRow, Evidence, RegistryKeyRow, RegistryRows, Report, StoredVehicles,
    VehicleRows, journey_json,
};
use super::{StoreError, StoreResult};
use crate::index::sql::PgDate;

/// The registry as `ref.*` holds it now (`load_registry`).
pub async fn load_registry(client: &Client) -> StoreResult<RegistryRows> {
    let mut registry = RegistryRows::default();
    for r in client
        .query(
            "SELECT k.vehicle_id, k.source, k.source_vehicle_id, k.scheme, k.identifier, \
             k.valid_from, k.valid_to, d.priority \
             FROM ref.vehicle_key k JOIN ref.dataset d USING (dataset) \
             ORDER BY 1, 2, 3, 4, 5, 6",
            &[],
        )
        .await?
    {
        registry.keys.push(RegistryKeyRow {
            vehicle_id: r.try_get(0)?,
            source: r.try_get(1)?,
            source_vehicle_id: r.try_get(2)?,
            scheme: r.try_get(3)?,
            identifier: r.try_get(4)?,
            valid_from: r.try_get::<_, PgDate>(5)?.0,
            valid_to: r.try_get::<_, Option<PgDate>>(6)?.map(|d| d.0),
            priority: i64::from(r.try_get::<_, i32>(7)?),
        });
    }
    for r in client
        .query(
            "SELECT x.vehicle_id, \
             (array_agg(x.public_label ORDER BY d.priority) \
              FILTER (WHERE x.public_label IS NOT NULL))[1], \
             (array_agg(m.manufacturer ORDER BY d.priority) \
              FILTER (WHERE m.manufacturer IS NOT NULL))[1], \
             (array_agg(m.model ORDER BY d.priority) FILTER (WHERE m.model IS NOT NULL))[1], \
             array_agg(x.dataset ORDER BY d.priority, x.dataset) \
             FROM ref.vehicle x JOIN ref.dataset d USING (dataset) \
             LEFT JOIN ref.vehicle_model m \
              ON m.vehicle_model_id = x.vehicle_model_id AND m.dataset = x.dataset \
             GROUP BY x.vehicle_id ORDER BY x.vehicle_id",
            &[],
        )
        .await?
    {
        registry.entries.push(EntryRow {
            vehicle_id: r.try_get(0)?,
            public_label: r.try_get(1)?,
            manufacturer: r.try_get(2)?,
            model: r.try_get(3)?,
            datasets: r.try_get(4)?,
        });
    }
    for r in client
        .query(
            "SELECT a.vehicle_id, a.attribute, a.value, a.dataset, d.priority, 'curated' \
             FROM ref.vehicle_attribute a JOIN ref.dataset d USING (dataset) \
             UNION ALL \
             SELECT x.vehicle_id, m.attribute, m.value, m.dataset, d.priority, 'model' \
             FROM ref.vehicle x JOIN ref.model_attribute m \
              ON m.vehicle_model_id = x.vehicle_model_id AND m.dataset = x.dataset \
             JOIN ref.dataset d ON d.dataset = m.dataset \
             UNION ALL \
             SELECT x.vehicle_id, v.attribute, v.value, mo.dataset, d.priority, 'model' \
             FROM ref.vehicle x JOIN ref.vehicle_model mo \
              ON mo.vehicle_model_id = x.vehicle_model_id AND mo.dataset = x.dataset \
             JOIN ref.dataset d ON d.dataset = mo.dataset \
             CROSS JOIN LATERAL (VALUES ('manufacturer', mo.manufacturer), \
              ('model', mo.model)) AS v (attribute, value) WHERE v.value IS NOT NULL \
             ORDER BY 1, 2, 5, 4",
            &[],
        )
        .await?
    {
        registry.values.push(CuratedValueRow {
            vehicle_id: r.try_get(0)?,
            attribute: r.try_get(1)?,
            value: r.try_get(2)?,
            dataset: r.try_get(3)?,
            priority: i64::from(r.try_get::<_, i32>(4)?),
            origin: r.try_get(5)?,
        });
    }
    Ok(registry)
}

fn journey_of(text: &str) -> StoreResult<JourneyKey> {
    let journey: serde_json::Value =
        serde_json::from_str(text).map_err(|e| StoreError::other(e.to_string()))?;
    let field = |key: &str| journey[key].as_str().unwrap_or_default().to_owned();
    Ok(JourneyKey {
        feed: field("feed").parse::<Feed>().map_err(StoreError::other)?,
        namespace: field("namespace"),
        key: field("key"),
        service_date: Date::from_isoformat(&field("service_date"))
            .map_err(|e| StoreError::other(e.0))?,
    })
}

/// The registry and the stored pairings and live attributes (`VehicleBook.load`'s reads).
pub async fn load_vehicles(client: &Client) -> StoreResult<StoredVehicles> {
    let registry = load_registry(client).await?;
    let mut pairings = Vec::new();
    for r in client
        .query(
            "SELECT source_a, source_vehicle_a, source_b, source_vehicle_b, first_seen, \
             last_seen, fixes, confirmed, journey FROM rt.vehicle_pairing ORDER BY 1, 2, 3, 4",
            &[],
        )
        .await?
    {
        let a: Report = (r.try_get(0)?, r.try_get(1)?);
        let b: Report = (r.try_get(2)?, r.try_get(3)?);
        pairings.push((
            (a, b),
            Evidence {
                first_seen: r.try_get::<_, PgTime>(4)?.0,
                last_seen: r.try_get::<_, PgTime>(5)?.0,
                fixes: i64::from(r.try_get::<_, i32>(6)?),
                journey: journey_of(&r.try_get::<_, JsonText>(8)?.0)?,
                confirmed: r.try_get(7)?,
            },
        ));
    }
    let mut attributes = Vec::new();
    for r in client
        .query(
            "SELECT source, source_vehicle_id, attribute, value, last_seen \
             FROM rt.vehicle_attribute_observed ORDER BY 1, 2, 3",
            &[],
        )
        .await?
    {
        attributes.push((
            (r.try_get(0)?, r.try_get(1)?),
            r.try_get(2)?,
            r.try_get(3)?,
            r.try_get::<_, PgTime>(4)?.0,
        ));
    }
    Ok(StoredVehicles {
        registry,
        pairings,
        attributes,
    })
}

async fn in_transaction<T>(
    client: &Client,
    body: impl Future<Output = Result<T, tokio_postgres::Error>>,
) -> StoreResult<T> {
    client.batch_execute("BEGIN").await?;
    match body.await {
        Ok(value) => {
            client.batch_execute("COMMIT").await?;
            Ok(value)
        }
        Err(error) => {
            let _ = client.batch_execute("ROLLBACK").await;
            Err(error.into())
        }
    }
}

/// Each source's `[vehicles] physical` as `rt.physical_vehicles`, replaced whole
/// (`write_physical_policy`).
pub async fn write_physical(client: &Client, rows: &[(String, String, bool)]) -> StoreResult<()> {
    in_transaction(client, async {
        client
            .execute("DELETE FROM rt.physical_vehicles", &[])
            .await?;
        let insert = client
            .prepare(
                "INSERT INTO rt.physical_vehicles (source, mode, physical) VALUES ($1, $2, $3)",
            )
            .await?;
        let all: Vec<Params> = rows
            .iter()
            .map(|(source, mode, physical)| -> Params { vec![source, mode, physical] })
            .collect();
        try_join_all(all.iter().map(|p| client.execute(&insert, p))).await?;
        Ok(())
    })
    .await
}

type Params<'a> = Vec<&'a (dyn ToSql + Sync)>;

async fn each(
    client: &Client,
    sql: &str,
    rows: &[Params<'_>],
) -> Result<(), tokio_postgres::Error> {
    if rows.is_empty() {
        return Ok(());
    }
    let statement = client.prepare(sql).await?;
    try_join_all(rows.iter().map(|p| client.execute(&statement, p))).await?;
    Ok(())
}

const ATTRIBUTE: &str = "INSERT INTO rt.vehicle_attribute_observed AS o (source, \
 source_vehicle_id, attribute, value, first_seen, last_seen) VALUES ($1, $2, $3, $4, $5, $5) \
 ON CONFLICT (source, source_vehicle_id, attribute) DO UPDATE SET \
 value = excluded.value, \
 first_seen = CASE WHEN o.value = excluded.value THEN o.first_seen \
  ELSE excluded.first_seen END, \
 last_seen = greatest(o.last_seen, excluded.last_seen)";

const PAIRING_GONE: &str = "DELETE FROM rt.vehicle_pairing WHERE (source_a, source_vehicle_a, \
 source_b, source_vehicle_b) = ($1, $2, $3, $4)";

const PAIRING: &str = "INSERT INTO rt.vehicle_pairing VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) \
 ON CONFLICT (source_a, source_vehicle_a, source_b, source_vehicle_b) \
 DO UPDATE SET (first_seen, last_seen, fixes, confirmed, journey) = \
 (excluded.first_seen, excluded.last_seen, excluded.fixes, excluded.confirmed, excluded.journey)";

const PROFILE: &str = "INSERT INTO rt.vehicle_profile (vehicle_id, registered, public_label, \
 manufacturer, model, low_floor, air_conditioning, wheelchair_accessible, operator, \
 licence_plate, fleet_number, datasets, sources, attributes, last_stated) \
 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) \
 ON CONFLICT (vehicle_id) DO UPDATE SET (registered, public_label, manufacturer, model, \
 low_floor, air_conditioning, wheelchair_accessible, operator, licence_plate, fleet_number, \
 datasets, sources, attributes, last_stated) = (excluded.registered, excluded.public_label, \
 excluded.manufacturer, excluded.model, excluded.low_floor, excluded.air_conditioning, \
 excluded.wheelchair_accessible, excluded.operator, excluded.licence_plate, \
 excluded.fleet_number, excluded.datasets, excluded.sources, excluded.attributes, \
 excluded.last_stated)";

/// The book's rows (`write_vehicle_rows`): live attributes, pairings, then profiles (all of
/// them deleted first on a rewrite). Runs inside the writer's transaction.
pub async fn write_vehicle_rows(client: &Client, rows: &VehicleRows) -> StoreResult<()> {
    let times: Vec<PgTime> = rows
        .attributes
        .iter()
        .map(|a| PgTime(a.observed_at))
        .collect();
    let attributes: Vec<Params> = rows
        .attributes
        .iter()
        .zip(&times)
        .map(|(a, at)| -> Params {
            vec![&a.source, &a.source_vehicle_id, &a.attribute, &a.value, at]
        })
        .collect();
    each(client, ATTRIBUTE, &attributes).await?;
    let gone: Vec<Params> = rows
        .gone
        .iter()
        .map(|(a, b)| -> Params { vec![&a.0, &a.1, &b.0, &b.1] })
        .collect();
    each(client, PAIRING_GONE, &gone).await?;
    let mut kept_values = Vec::with_capacity(rows.kept.len());
    for (_, e) in &rows.kept {
        let fixes = i32::try_from(e.fixes).map_err(|e| StoreError::other(e.to_string()))?;
        let journey = Jsonb(dumps(&journey_json(&e.journey), DumpOptions::DEFAULT));
        kept_values.push((PgTime(e.first_seen), PgTime(e.last_seen), fixes, journey));
    }
    let kept: Vec<Params> = rows
        .kept
        .iter()
        .zip(&kept_values)
        .map(|(((a, b), e), (first, last, fixes, journey))| -> Params {
            vec![
                &a.0,
                &a.1,
                &b.0,
                &b.1,
                first,
                last,
                fixes,
                &e.confirmed,
                journey,
            ]
        })
        .collect();
    each(client, PAIRING, &kept).await?;
    if rows.rewrite {
        client
            .execute("DELETE FROM rt.vehicle_profile", &[])
            .await?;
    }
    let profile_values: Vec<_> = rows
        .profiles
        .iter()
        .map(|p| {
            (
                p.text("manufacturer"),
                p.text("model"),
                p.flag("low_floor"),
                p.flag("air_conditioning"),
                p.flag("wheelchair_accessible"),
                p.text("operator"),
                p.text("licence_plate"),
                p.text("fleet_number"),
                Jsonb(dumps(
                    &PyJson::List(p.attributes.clone()),
                    DumpOptions::DEFAULT,
                )),
                p.last_stated.map(PgTime),
            )
        })
        .collect();
    let profiles: Vec<Params> = rows
        .profiles
        .iter()
        .zip(&profile_values)
        .map(|(p, v)| -> Params {
            vec![
                &p.vehicle_id,
                &p.registered,
                &p.public_label,
                &v.0,
                &v.1,
                &v.2,
                &v.3,
                &v.4,
                &v.5,
                &v.6,
                &v.7,
                &p.datasets,
                &p.sources,
                &v.8,
                &v.9,
            ]
        })
        .collect();
    each(client, PROFILE, &profiles).await?;
    Ok(())
}
