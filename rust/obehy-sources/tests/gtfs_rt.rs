//! The GTFS-RT code decoders through the shipped manifests: IDS JMK vehicles
//! (`gtfs-rt-vehicles`) and alerts (`gtfs-rt-alerts`), one test per quirk
//! (docs/sources/ids-jmk.md, Quirks), and how a manifest that misuses them fails.

use std::path::PathBuf;

use obehy_core::alerts::AlertEntity;
use obehy_core::config::decode::Decode;
use obehy_core::config::manifest::{self, RECORDER_FILTERS};
use obehy_core::model::{
    Fact, Feed, Observation, Position, ServiceDay, TripKey, VehicleAttribute, VehicleKey,
};
use obehy_core::time::{Date, Instant, TimeDelta};
use obehy_sources::reader::gtfs_rt::{
    Alert, EntitySelector, FeedEntity, FeedMessage, TripDescriptor, VehicleDescriptor,
    VehiclePosition,
};
use obehy_sources::{Decoder, Decoders};
use prost::Message;

const NOW: i64 = 1_795_552_345;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn decoders() -> Decoders {
    let sources = manifest::load_sources(
        &repo().join("src/obehy/data/realtime/sources"),
        &RECORDER_FILTERS,
    )
    .unwrap();
    let package = repo().join("src/obehy");
    let files = |name: &str| std::fs::read_to_string(package.join(name)).map_err(|e| e.to_string());
    Decoders::new(&sources, &files).unwrap()
}

fn vehicle(id: &str, label: &str, trip: Option<&str>, at: u64) -> FeedEntity {
    FeedEntity {
        id: Some(format!("{id}_24213225")),
        vehicle: Some(VehiclePosition {
            trip: trip.map(|t| TripDescriptor {
                trip_id: Some(t.into()),
                ..Default::default()
            }),
            position: Some(obehy_sources::reader::gtfs_rt::Position {
                latitude: Some(49.213_005),
                longitude: Some(16.560_734),
                bearing: Some(112.0),
            }),
            timestamp: Some(at),
            vehicle: Some(VehicleDescriptor {
                id: Some(id.into()),
                label: Some(label.into()),
            }),
        }),
        ..Default::default()
    }
}

fn observations(entities: Vec<FeedEntity>) -> Vec<Observation> {
    let body = FeedMessage { entity: entities }.encode_to_vec();
    decoders()
        .decode(
            "ids-jmk",
            "vehicles",
            &body,
            "ff",
            Instant::from_unix_seconds(NOW),
            TimeDelta::from_micros(1_200_000_000),
            None,
        )
        .unwrap()
}

fn position() -> Fact {
    Fact::Position(Position {
        lat: 49.213_005,
        lon: 16.560_734,
        bearing: Some(112.0),
    })
}

#[test]
fn idsjmk_q2_the_descriptor_id_keys_the_vehicle_and_its_label_is_the_fleet_number() {
    let found = observations(vec![vehicle(
        "10690",
        "1069",
        Some("22905"),
        NOW as u64 - 8,
    )]);
    let [o] = found.as_slice() else {
        panic!("{found:?}")
    };
    assert_eq!(
        (o.feed, o.observed_at, o.decoder_version, o.raw.item),
        (Feed::Jdf, Some(Instant::from_unix_seconds(NOW - 8)), 1, 0)
    );
    assert_eq!(
        o.facts,
        [
            Fact::VehicleKey(VehicleKey {
                source_vehicle_id: "10690".into()
            }),
            Fact::TripKey(TripKey {
                namespace: "ids-jmk:gtfs_trip_line_course".into(),
                key: "22905".into()
            }),
            position(),
            Fact::VehicleAttribute(VehicleAttribute {
                attribute: "fleet_number".into(),
                value: "1069".into()
            }),
        ]
    );
}

#[test]
fn idsjmk_q9_a_trip_id_is_read_as_its_run_the_trip_of_its_line_and_course_that_day() {
    let found = observations(vec![vehicle("10690", "1069", Some("26870"), NOW as u64)]);
    let trips: Vec<&Fact> = found[0]
        .facts
        .iter()
        .filter(|f| matches!(f, Fact::TripKey(_)))
        .collect();
    assert_eq!(
        trips,
        [&Fact::TripKey(TripKey {
            namespace: "ids-jmk:gtfs_trip_line_course".into(),
            key: "26870".into()
        })]
    );
}

#[test]
fn idsjmk_q3_no_delay_is_stated() {
    let found = observations(vec![vehicle("10690", "1069", Some("22905"), NOW as u64)]);
    assert!(!found[0].facts.iter().any(|f| matches!(f, Fact::Delay(_))));
}

#[test]
fn idsjmk_q7_a_fix_beyond_the_clock_skew_carries_no_position() {
    let found = observations(vec![vehicle(
        "20170",
        "2017",
        Some("39510"),
        NOW as u64 - 3_600,
    )]);
    assert_eq!(found[0].observed_at, None);
    assert!(!found[0].facts.contains(&position()));
    assert!(found[0].facts.iter().any(|f| matches!(f, Fact::TripKey(_))));
}

#[test]
fn idsjmk_q8_a_vehicle_without_a_trip_is_stated_alone() {
    let found = observations(vec![vehicle("10720", "1072", None, NOW as u64)]);
    assert_eq!(
        found[0].facts[..2],
        [
            Fact::VehicleKey(VehicleKey {
                source_vehicle_id: "10720".into()
            }),
            position(),
        ]
    );
}

#[test]
fn a_start_date_is_the_service_day_and_unusable_entities_are_skipped() {
    let mut dated = vehicle("10820", "1082", Some("26055"), NOW as u64);
    dated
        .vehicle
        .as_mut()
        .unwrap()
        .trip
        .as_mut()
        .unwrap()
        .start_date = Some("20261124".into());
    let mut deleted = vehicle("10830", "1083", Some("23125"), NOW as u64);
    deleted.is_deleted = Some(true);
    let mut nameless = vehicle("10880", "1088", Some("23126"), NOW as u64);
    nameless.vehicle.as_mut().unwrap().vehicle = None;
    let alert = FeedEntity {
        alert: Some(Alert::default()),
        ..Default::default()
    };
    let found = observations(vec![alert, dated, deleted, nameless]);
    let [o] = found.as_slice() else {
        panic!("{found:?}")
    };
    assert_eq!(o.raw.item, 1);
    assert!(o.facts.contains(&Fact::ServiceDay(ServiceDay {
        day: Date::ymd(2026, 11, 24)
    })));
}

#[test]
fn idsjmk_q4_alerts_name_their_entities_in_the_ids_jmk_namespaces() {
    let selector = |route: Option<&str>, stop: Option<&str>, trip: Option<&str>| EntitySelector {
        route_id: route.map(Into::into),
        stop_id: stop.map(Into::into),
        trip: trip.map(|t| TripDescriptor {
            trip_id: Some(t.into()),
            start_date: Some("20261124".into()),
        }),
    };
    let body = FeedMessage {
        entity: vec![
            vehicle("10690", "1069", Some("22905"), NOW as u64),
            FeedEntity {
                id: Some("a1".into()),
                alert: Some(Alert {
                    informed_entity: vec![
                        selector(Some("423"), None, None),
                        selector(Some("423"), Some("U1621Z2"), None),
                        selector(None, None, Some("22905")),
                        selector(Some("423"), None, None),
                    ],
                    effect: Some(9),
                    ..Default::default()
                }),
                ..Default::default()
            },
        ],
    }
    .encode_to_vec();
    let alerts = decoders()
        .decode_alerts("ids-jmk", "alerts", &body, Instant::from_unix_seconds(NOW))
        .unwrap();
    let [a] = alerts.as_slice() else {
        panic!("{alerts:?}")
    };
    let entity = |kind: &str, ns: &str, id: &str, day| AlertEntity {
        kind: kind.into(),
        namespace: ns.into(),
        identifier: id.into(),
        service_date: day,
    };
    assert_eq!(
        (a.source.as_str(), a.channel.as_str(), a.effect.as_str()),
        ("ids-jmk", "alerts", "STOP_MOVED")
    );
    assert_eq!(
        a.entities,
        [
            entity("route", "ids-jmk:gtfs_route_id", "423", None),
            entity("stop", "ids-jmk:gtfs_stop_id", "U1621Z2", None),
            entity(
                "trip",
                "ids-jmk:gtfs_trip_line_course",
                "22905",
                Some(Date::ymd(2026, 11, 24))
            ),
        ]
    );
}

#[test]
fn a_gtfs_rt_decoder_needs_its_reader_and_keys() {
    let compiles = |decode: &str| {
        let decode: Decode = toml::from_str(decode).unwrap();
        Decoder::compile(&decode, "x", "v", &|_: &str| Err("no files".to_owned())).map(|_| ())
    };
    let good = "version = 1
reader = \"gtfs-rt\"
decoder = \"gtfs-rt-vehicles\"
                feed = \"jdf\"
keys = { trip = \"x:gtfs_trip_id\" }
";
    assert!(compiles(good).is_ok());
    for (bad, why) in [
        (good.replace("\"gtfs-rt\"", "\"json\""), "reader"),
        (good.replace("trip = ", "stop = "), "keys.trip"),
        (
            good.replace(
                "version = 1
",
                "",
            ),
            "version",
        ),
        (
            good.replace(
                "feed = \"jdf\"
",
                "",
            ),
            "feed",
        ),
    ] {
        let error = compiles(&bad).unwrap_err().0;
        assert!(error.contains(why), "{why}: {error}");
    }
    let alerts = "reader = \"gtfs-rt\"
decoder = \"gtfs-rt-alerts\"
";
    assert!(compiles(alerts).unwrap_err().0.contains("keys"));
}
