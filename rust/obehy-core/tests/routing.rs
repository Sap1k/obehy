//! IDSJMK-Q1: a `trip_key_feed` channel's observations go to the feed whose static data holds
//! their trip key; one without a trip key follows its vehicle.

mod common;

use std::collections::BTreeMap;
use std::sync::Arc;

use obehy_core::config::Policy;
use obehy_core::config::policy::POLICY_FILE;
use obehy_core::core::Semantics;
use obehy_core::index::{
    CallRow, KeyRow, LocationRow, Rows, RowsError, RunPartRow, ShapePointRow, TripRow,
};
use obehy_core::model::{Fact, Feed, Observation, RawRef, SourceSemantics, TripKey, VehicleKey};
use obehy_core::runner::{Loads, Runner, Untimed};
use obehy_core::time::{Date, Instant};

const NAMESPACE: &str = "ids-jmk:gtfs_trip_line_course";

/// A feed whose static data is these keys and nothing else.
#[derive(Debug, Clone, Default)]
struct KeysOnly(Vec<KeyRow>);

impl Rows for KeysOnly {
    fn keys(
        &mut self,
        namespaces: &[&str],
        identifiers: &[&str],
    ) -> Result<Vec<KeyRow>, RowsError> {
        let wanted: Vec<(&str, &str)> = namespaces
            .iter()
            .copied()
            .zip(identifiers.iter().copied())
            .collect();
        Ok(self
            .0
            .iter()
            .filter(|k| wanted.contains(&(&*k.namespace, &*k.identifier)))
            .cloned()
            .collect())
    }
    fn line_keys(&mut self, _: &str, _: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        Ok(Vec::new())
    }
    fn trips(&mut self, _: &[&str]) -> Result<Vec<TripRow>, RowsError> {
        Ok(Vec::new())
    }
    fn calls(&mut self, _: &[&str]) -> Result<Vec<CallRow>, RowsError> {
        Ok(Vec::new())
    }
    fn run_parts(&mut self, _: &[&str]) -> Result<Vec<RunPartRow>, RowsError> {
        Ok(Vec::new())
    }
    fn station_keys(&mut self, _: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        Ok(Vec::new())
    }
    fn track_keys(&mut self, _: &[&str]) -> Result<Vec<(String, String)>, RowsError> {
        Ok(Vec::new())
    }
    fn locations(&mut self, _: &[&str]) -> Result<Vec<LocationRow>, RowsError> {
        Ok(Vec::new())
    }
    fn shape_points(&mut self, _: &[&str]) -> Result<Vec<ShapePointRow>, RowsError> {
        Ok(Vec::new())
    }
    fn service_dates(&mut self, _: &[&str], _: &[Date]) -> Result<Vec<(String, Date)>, RowsError> {
        Ok(Vec::new())
    }
}

#[derive(Debug, Clone)]
struct Release(BTreeMap<Feed, KeysOnly>);

impl Loads for Release {
    type Rows = KeysOnly;
    fn release_id(&self) -> &str {
        "test"
    }
    fn rows(&self, feed: Feed) -> Result<KeysOnly, RowsError> {
        Ok(self.0.get(&feed).cloned().unwrap_or_default())
    }
}

fn observation(channel: &str, vehicle: &str, trip: Option<&str>) -> Arc<Observation> {
    let mut facts = vec![Fact::VehicleKey(VehicleKey {
        source_vehicle_id: vehicle.to_owned(),
    })];
    if let Some(trip) = trip {
        facts.push(Fact::TripKey(TripKey {
            namespace: NAMESPACE.to_owned(),
            key: trip.to_owned(),
        }));
    }
    Arc::new(Observation {
        source: "ids-jmk".to_owned(),
        channel: channel.to_owned(),
        feed: Feed::Jdf,
        received_at: Instant::from_micros(1_795_552_337_000_000),
        observed_at: None,
        raw: RawRef {
            sha256: "0".to_owned(),
            item: 0,
        },
        decoder_version: 1,
        facts,
    })
}

#[test]
fn idsjmk_q1_an_observation_goes_to_the_feed_holding_its_trip_key() {
    let policy = Policy::load(
        &common::repo()
            .join("src/obehy/data/realtime")
            .join(POLICY_FILE),
    )
    .unwrap();
    let train = KeyRow {
        namespace: NAMESPACE.to_owned(),
        identifier: "443".to_owned(),
        public_id: "czptt:4330".to_owned(),
        entity_kind: "trip".to_owned(),
        valid_from: Date::ymd(2026, 11, 24),
        valid_to: Date::ymd(2026, 11, 24),
        service_id: Some("s".to_owned()),
    };
    let release = Release(BTreeMap::from([(Feed::Czptt, KeysOnly(vec![train]))]));
    let mut semantics = Semantics::new();
    semantics.insert(
        ("ids-jmk".to_owned(), "vehicles".to_owned()),
        SourceSemantics {
            trip_key_feed: Some(Feed::Czptt),
            ..Default::default()
        },
    );
    let mut runner = Runner::new(
        release,
        Arc::new(policy),
        &[Feed::Jdf, Feed::Czptt],
        semantics,
        Untimed,
    )
    .unwrap();
    let feeds = |runner: &mut Runner<Release>, observations: &[Arc<Observation>]| {
        runner
            .route(observations)
            .unwrap()
            .iter()
            .map(|o| o.feed)
            .collect::<Vec<_>>()
    };
    // A train's key is held by czptt, a bus's is not; the train's unit then names no trip and
    // stays on czptt, the bus on jdf. A vehicle never routed stays on the decoder's feed.
    assert_eq!(
        feeds(
            &mut runner,
            &[
                observation("vehicles", "unit", Some("443")),
                observation("vehicles", "bus", Some("22905")),
            ]
        ),
        [Feed::Czptt, Feed::Jdf]
    );
    assert_eq!(
        feeds(
            &mut runner,
            &[
                observation("vehicles", "unit", None),
                observation("vehicles", "bus", None),
                observation("vehicles", "parked", None),
            ]
        ),
        [Feed::Czptt, Feed::Jdf, Feed::Jdf]
    );
    // The unit's next trip is a bus's key: it follows the key, and its trip-less entries after.
    assert_eq!(
        feeds(
            &mut runner,
            &[
                observation("vehicles", "unit", Some("22905")),
                observation("vehicles", "unit", None),
            ]
        ),
        [Feed::Jdf, Feed::Jdf]
    );
    // Routed observations routed again stay where they are.
    let routed = runner
        .route(&[
            observation("vehicles", "unit", Some("443")),
            observation("vehicles", "unit", None),
            observation("vehicles", "bus", Some("22905")),
        ])
        .unwrap();
    assert_eq!(
        feeds(&mut runner, &routed),
        [Feed::Czptt, Feed::Czptt, Feed::Jdf]
    );
    // A channel without trip_key_feed keeps its decoder's feed.
    assert_eq!(
        feeds(&mut runner, &[observation("alerts", "unit", Some("443"))]),
        [Feed::Jdf]
    );
}
