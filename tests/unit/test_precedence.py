"""Which vehicle a report is, and who wins when statements about it clash
(reference/registry.py, reference/precedence.py, data/vehicles/attributes.toml)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from obehy.reference.precedence import Statement, pick, profile
from obehy.reference.registry import CuratedValue, Entry, Registry, RegistryKey
from obehy.reference.vocabulary import load_vocabulary

VOCABULARY = load_vocabulary()
EARLY = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
LATE = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
DAY = date(2026, 10, 20)


def _registry() -> Registry:
    """duk:807 known by its DÚK ID (the seed) and its plate (our list); obehy:9 by a plate that
    was another bus's until 2026-06-30; obehy:650002 by its series number (a 55p unit)."""

    def key(vehicle_id: str, priority: int, since: date, until: date | None = None) -> RegistryKey:
        return RegistryKey(vehicle_id, priority, since, until)

    return Registry(
        by_source={("duk", "807"): [key("duk:807", 10, date(2000, 1, 1))]},
        by_identifier={
            ("licence_plate", "4U12345"): [key("duk:807", 0, date(2000, 1, 1))],
            ("licence_plate", "1A11111"): [
                key("obehy:8", 0, date(2000, 1, 1), date(2026, 6, 30)),
                key("obehy:9", 0, date(2026, 7, 1)),
            ],
            ("series_number", "650002"): [key("obehy:650002", 0, date(2000, 1, 1))],
        },
    )


def test_a_report_is_its_vehicle_by_source_key_else_by_an_identifier() -> None:
    registry = _registry()
    assert registry.bound("duk", "807", {}, DAY) == ("duk:807", "source")
    # MPVnet sees the same bus: the plate key makes it duk:807 too, written any way.
    assert registry.bound("mpvnet", "4U1 2345", {"licence_plate": "4u1 23-45"}, DAY) == (
        "duk:807",
        "identifier",
    )
    # A plate passed on: the key valid that day.
    plate = {"licence_plate": "1A1 1111"}
    assert registry.bound("arriva-express", "1A1 1111", plate, DAY) == ("obehy:9", "identifier")
    assert registry.bound("arriva-express", "1A1 1111", plate, date(2026, 1, 5)) == (
        "obehy:8",
        "identifier",
    )
    # A 55p unit by its series number, written as 55p writes it.
    assert registry.bound("55p", "650.002", {"series_number": "650.002"}, DAY) == (
        "obehy:650002",
        "identifier",
    )
    assert registry.bound("pid", "3:6940", {}, DAY) is None


def _live(attribute: str, value: str, source: str, at: datetime) -> Statement:
    return Statement(attribute, value, "live", source, observed_at=at)


def _curated(attribute: str, value: str, dataset: str, priority: int) -> Statement:
    return Statement(attribute, value, "curated", dataset, priority)


def test_the_registry_wins_and_a_clash_is_flagged_not_dropped() -> None:
    picked = pick(
        [
            _curated("low_floor", "true", "duk-register", 10),
            _live("low_floor", "false", "duk", LATE),
        ],
        VOCABULARY,
    )["low_floor"]
    assert (picked.winner.value, picked.winner.origin, picked.conflict) == ("true", "curated", True)
    assert [s.value for s in picked.statements] == ["true", "false"]


def test_our_list_beats_a_seed() -> None:
    picked = pick(
        [
            _curated("operator", "AKV bus", "duk-register", 10),
            _curated("operator", "Arriva", "vehicles", 0),
        ],
        VOCABULARY,
    )["operator"]
    assert (picked.winner.source, picked.conflict) == ("vehicles", True)


def test_live_sources_rank_by_the_vocabulary_then_recency() -> None:
    # air_conditioning has no order of its own: live_sources puts PID before DÚK.
    picked = pick(
        [
            _live("air_conditioning", "false", "duk", LATE),
            _live("air_conditioning", "true", "pid", EARLY),
            Statement("air_conditioning", "true", "model", "duk-register", 10),
        ],
        VOCABULARY,
    )["air_conditioning"]
    assert (picked.winner.source, picked.conflict) == ("pid", True)
    picked = pick(
        [
            _live("licence_plate", "1A1 1111", "mpvnet", EARLY),
            _live("licence_plate", "1A1 1112", "mpvnet", LATE),
        ],
        VOCABULARY,
    )["licence_plate"]
    assert picked.winner.value == "1A1 1112"  # two of one rank: the most recent


def test_a_profile_combines_the_registry_and_every_live_report() -> None:
    entry = Entry("duk:807", "807", None, "Crossway LE", ("vehicles", "duk-register"))
    found = profile(
        "duk:807",
        entry,
        [
            CuratedValue("low_floor", "true", "duk-register", 10, "curated"),
            CuratedValue("model", "Crossway LE", "duk-register", 10, "model"),
        ],
        [
            _live("low_floor", "false", "duk", EARLY),
            _live("licence_plate", "4U1 2345", "mpvnet", LATE),
        ],
        VOCABULARY,
    )
    assert found.registered and found.datasets == ("vehicles", "duk-register")
    assert found.flags["low_floor"] is True and found.flags["air_conditioning"] is None
    assert (found.texts["model"], found.texts["licence_plate"]) == ("Crossway LE", "4U1 2345")
    assert (found.sources, found.last_stated) == (("duk", "mpvnet"), LATE)
    low_floor = next(a for a in found.attributes if a["attribute"] == "low_floor")
    assert (low_floor["origin"], low_floor["conflict"], len(low_floor["statements"])) == (
        "curated",
        True,
        2,
    )
