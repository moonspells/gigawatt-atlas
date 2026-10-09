from __future__ import annotations

import pytest

from atlas.crosswalk import (
    EPOCH_CONSTRUCTION,
    EPOCH_PRE_CONSTRUCTION,
    UnknownStatus,
    from_aigridwatch_stage,
    from_epoch_row,
    from_osm_tags,
)


@pytest.mark.parametrize(
    ("tags", "status"),
    [
        ({"proposed:telecom": "data_center"}, "proposed"),
        ({"construction:telecom": "data_center"}, "under_construction"),
        ({"building": "construction", "construction": "data_center"}, "under_construction"),
        ({"telecom": "data_center"}, "operating"),
        ({"building": "data_center"}, "operating"),
    ],
)
def test_osm_tags(tags: dict[str, str], status: str) -> None:
    got = from_osm_tags(tags)
    assert got is not None
    assert (got.status, got.event, got.confidence) == (status, "first_reported", 0.60)
    assert got.evidence_level is None


def test_osm_precedence_construction_beats_telecom() -> None:
    got = from_osm_tags(
        {"telecom": "data_center", "building": "construction", "construction": "data_center"}
    )
    assert got is not None
    assert got.status == "under_construction"
    assert got.label == "construction=data_center"


def test_osm_precedence_proposed_beats_everything() -> None:
    got = from_osm_tags({"proposed:telecom": "data_center", "telecom": "data_center"})
    assert got is not None and got.status == "proposed"


def test_osm_unrelated_tags() -> None:
    assert from_osm_tags({"building": "warehouse"}) is None
    assert from_osm_tags({"building": "construction"}) is None


@pytest.mark.parametrize(
    ("stage", "has_filing", "status", "event", "evidence", "reason"),
    [
        ("Rumored", False, "announced", "announced", "rumor", None),
        ("Proposed", False, "announced", "announced", None, None),
        ("Proposed", True, "proposed", "application_filed", None, None),
        ("In review", False, "proposed", "application_filed", None, None),
        ("Hearing scheduled", False, "proposed", "application_filed", None, None),
        ("Awaiting decision", False, "proposed", "application_filed", None, None),
        ("Approved", False, "permitted", "approved", None, None),
        ("Under construction", False, "under_construction", "construction_start", None, None),
        ("Operating", False, "operating", "energized", None, None),
        ("Blocked by ban", False, "paused", "paused", None, "moratorium"),
        ("Denied", False, "denied", "denied", None, "local_denial"),
        # Who withdrew is not in the stage (a developer, a mayor, a lapsed agreement, a court).
        ("Withdrawn", False, "cancelled", "withdrawn", None, None),
    ],
)
def test_aigridwatch_stages(
    stage: str, has_filing: bool, status: str, event: str, evidence: str | None, reason: str | None
) -> None:
    got = from_aigridwatch_stage(stage, has_filing=has_filing)
    assert (got.status, got.event, got.evidence_level, got.status_reason) == (
        status,
        event,
        evidence,
        reason,
    )
    assert got.confidence == 0.70
    assert got.label == stage


def test_aigridwatch_stage_matching_ignores_case_and_spacing() -> None:
    assert from_aigridwatch_stage("  under   CONSTRUCTION ", has_filing=False).status == (
        "under_construction"
    )


def test_aigridwatch_unknown_stage() -> None:
    with pytest.raises(UnknownStatus):
        from_aigridwatch_stage("Mothballed", has_filing=False)


@pytest.mark.parametrize(
    ("text", "operational", "status", "event"),
    [
        ("Phase 1 energized", 2, "operating", "energized"),
        ("Construction ongoing", 0.5, "operating", "energized"),
        ("Project announced by the developer", 0, "announced", "announced"),
        ("Site acquired; rezoning pending", None, "announced", "announced"),
        (
            "Land clearing visible in satellite imagery",
            0,
            "under_construction",
            "construction_start",
        ),
        ("Announced in January; grading has begun", 0, "under_construction", "construction_start"),
        ("Satellite imagery shows activity", 0, "under_construction", "construction_start"),
    ],
)
def test_epoch_rows(text: str, operational: float | None, status: str, event: str) -> None:
    got = from_epoch_row(text, operational)
    assert (got.status, got.event, got.confidence, got.label) == (status, event, 0.70, text)


def test_epoch_regexes() -> None:
    assert EPOCH_CONSTRUCTION.search("excavation started")
    assert EPOCH_CONSTRUCTION.search("Retrofitting the mill")
    assert not EPOCH_CONSTRUCTION.search("constructive talks")
    assert EPOCH_PRE_CONSTRUCTION.search("PERMIT APPLICATION filed")
    assert not EPOCH_PRE_CONSTRUCTION.search("permits issued")


# Tag sets of four OpenStreetMap ways (lifecycle, telecom and name keys), from the Overpass extract
# of 2026-10-07T23:27:38Z; (c) OpenStreetMap contributors, ODbL 1.0. The old crosswalk mapped all
# four, and 20 more objects like them, to operating (review finding A1).
OSM_LIFECYCLE = [
    (
        "way/1173537117",
        {"building": "construction", "name": "Microsoft EAT04", "telecom": "data_center"},
        "under_construction",
        "building=construction",
    ),
    (
        "way/1314355599",
        {"landuse": "construction", "name": "Switch Las Vegas 12", "telecom": "data_center"},
        "under_construction",
        "landuse=construction",
    ),
    (
        "way/1501824326",
        {"name": "Percheron DC", "proposed:building": "industrial", "telecom": "data_center"},
        "proposed",
        "proposed:building=industrial",
    ),
    (
        "way/1549253249",
        {"industrial": "yes", "proposed": "yes", "telecom": "data_center"},
        "proposed",
        "proposed=yes",
    ),
]


@pytest.mark.parametrize(("ref", "tags", "status", "label"), OSM_LIFECYCLE)
def test_osm_lifecycle_tags_beat_telecom(
    ref: str, tags: dict[str, str], status: str, label: str
) -> None:
    got = from_osm_tags(tags)
    assert got is not None, ref
    assert (got.status, got.event, got.label) == (status, "first_reported", label), ref


@pytest.mark.parametrize(
    ("tags", "status", "label"),
    [
        (
            {"telecom": "data_center", "construction:building": "yes"},
            "under_construction",
            "construction:building=yes",
        ),
        ({"building": "data_center", "landuse": "construction"}, "under_construction", None),
        ({"building": "data_center", "proposed": "yes"}, "proposed", None),
        ({"telecom": "data_center", "proposed": "no"}, "operating", "telecom=data_center"),
        (
            {"telecom": "data_center", "construction:building": "no"},
            "operating",
            "telecom=data_center",
        ),
        ({"telecom": "data_center", "landuse": "industrial"}, "operating", "telecom=data_center"),
        (
            {"construction:building": "data_center"},
            "under_construction",
            "construction:building=data_center",
        ),
        ({"proposed:building": "data_center"}, "proposed", "proposed:building=data_center"),
    ],
)
def test_osm_lifecycle_variants(tags: dict[str, str], status: str, label: str | None) -> None:
    got = from_osm_tags(tags)
    assert got is not None and got.status == status
    if label is not None:
        assert got.label == label


def test_osm_lifecycle_tags_alone_are_not_a_data_center() -> None:
    for tags in (
        {"proposed": "yes"},
        {"landuse": "construction"},
        {"proposed:building": "industrial"},
        {"construction:building": "yes"},
    ):
        assert from_osm_tags(tags) is None, tags


def test_osm_proposed_beats_construction() -> None:
    """Both tagged: the less advanced status, so the map never overstates progress."""
    for tags in (
        {"proposed:telecom": "data_center", "construction": "data_center"},
        {"proposed:telecom": "data_center", "building": "construction"},
        {"telecom": "data_center", "proposed": "yes", "building": "construction"},
    ):
        got = from_osm_tags(tags)
        assert got is not None and got.status == "proposed", tags


@pytest.mark.parametrize(
    ("operational", "it_mw", "status"),
    [
        (None, 857.0, "operating"),
        (None, 0.0, "under_construction"),
        (None, None, "under_construction"),
        (0, 857.0, "under_construction"),
        (2, None, "operating"),
    ],
)
def test_epoch_empty_building_count_falls_back_to_it_power(
    operational: float | None, it_mw: float | None, status: str
) -> None:
    """SV-15: OpenAI Stargate Milam's 2028-12-31 row has an empty building count, 857 MW of IT
    power and the text 'All buildings finished, site is fully operational'."""
    text = "All buildings finished, site is fully operational."
    assert from_epoch_row(text, operational, it_mw=it_mw).status == status


def test_epoch_it_power_is_keyword_only_and_optional() -> None:
    assert from_epoch_row("Grading underway", None).status == "under_construction"
