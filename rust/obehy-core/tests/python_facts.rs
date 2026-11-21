//! Facts JSON parity with `model_json.py`: every observation `tools/gen_fixtures.py` wrote
//! (hand-made ones covering every fact type and optional field, and real ones decoded from
//! archived payloads of every source with an archive) reads back and re-serializes to the
//! byte-identical text, both as stored (`ensure_ascii`) and as UTF-8.

mod common;

use std::collections::BTreeSet;

use obehy_core::model::json::{facts_from_json_text, facts_json_text, facts_to_json};
use obehy_core::model::{Fact, Feed, Observation, RawRef};
use obehy_core::py::json::{DumpOptions, dumps};
use obehy_core::time::Instant;
use serde_json::Value;

fn kind(fact: &Fact) -> &'static str {
    match fact {
        Fact::VehicleKey(_) => "vehicle_key",
        Fact::TripKey(_) => "trip_key",
        Fact::LineRef(_) => "line_ref",
        Fact::Destination(_) => "destination",
        Fact::NextStopName(_) => "next_stop_name",
        Fact::Position(_) => "position",
        Fact::Delay(_) => "delay",
        Fact::SourceState(_) => "source_state",
        Fact::SourceDeparture(_) => "source_departure",
        Fact::ServiceDay(_) => "service_day",
        Fact::PointEvent(_) => "point_event",
        Fact::NextPoint(_) => "next_point",
        Fact::NextStopPrediction(_) => "next_stop_prediction",
        Fact::TripStatus(_) => "trip_status",
        Fact::TripCancelled(_) => "trip_cancelled",
        Fact::PlatformAssignment(_) => "platform",
        Fact::VehicleAttribute(_) => "vehicle_attribute",
    }
}

#[test]
fn every_fixture_observation_round_trips_byte_identically() {
    let text = common::text("facts.jsonl");
    let mut kinds = BTreeSet::new();
    let mut sources = BTreeSet::new();
    let mut count = 0;
    for line in text.lines() {
        let entry: Value = serde_json::from_str(line).unwrap();
        let stored = entry["facts"].as_str().unwrap();
        let facts = facts_from_json_text(stored).unwrap_or_else(|e| panic!("{e}: {stored}"));
        assert_eq!(facts_json_text(&facts), stored);
        assert_eq!(
            dumps(&facts_to_json(&facts), DumpOptions::COMPACT.utf8()),
            entry["facts_utf8"].as_str().unwrap()
        );
        let received = entry["received_at"].as_str().unwrap();
        let observation = Observation {
            source: entry["source"].as_str().unwrap().to_owned(),
            channel: entry["channel"].as_str().unwrap().to_owned(),
            feed: entry["feed"].as_str().unwrap().parse::<Feed>().unwrap(),
            received_at: Instant::from_isoformat(received).unwrap(),
            observed_at: entry["observed_at"]
                .as_str()
                .map(|t| Instant::from_isoformat(t).unwrap()),
            raw: RawRef {
                sha256: entry["sha256"].as_str().unwrap().to_owned(),
                item: entry["item"].as_i64().unwrap(),
            },
            decoder_version: entry["decoder_version"].as_i64().unwrap(),
            facts,
        };
        assert_eq!(observation.received_at.isoformat(), received);
        assert_eq!(
            observation.observed_at.map(|t| t.isoformat()).as_deref(),
            entry["observed_at"].as_str()
        );
        kinds.extend(observation.facts.iter().map(kind));
        sources.insert(observation.source.clone());
        count += 1;
    }
    assert_eq!(kinds.len(), 17, "fact types covered: {kinds:?}");
    assert!(count > 500, "{count} observations");
    assert!(sources.len() > 2, "{sources:?}");
}
