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
        ("Withdrawn", False, "cancelled", "withdrawn", None, "developer_withdrawal"),
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
