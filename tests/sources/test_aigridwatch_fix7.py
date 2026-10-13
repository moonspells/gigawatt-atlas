"""AI GridWatch rules of fix round 7, each measured on the seed's inputs against the round 6
sample (the record a rule fixes is named by its record id there).

tests/fixtures/aigridwatch/fix7-2026-10-08.json holds Google West Memphis's row (README.md there
says what was changed); the other cases are rows written here from the seed's entries. The rows
are read on 2026-10-09, the day of the seed import.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.sources.aigridwatch import (
    IMPORTER,
    first_report,
    log_milestone,
    milestone_events,
    names_project,
    tied_llcs,
)
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.validate import validate_record

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch"
FIX7 = SAMPLES / "fix7-2026-10-08.json"
# Fixture runs read no entry of the committed config/overrides/aigridwatch.json (as
# test_aigridwatch_fix6.py says).
NO_ENTRIES = SAMPLES / "overrides-none.json"
NOW = datetime(2026, 10, 9, 9, 52, 35, tzinfo=UTC)
TODAY = NOW.date()
MakeContext = Callable[..., ImportContext]
MEMPHIS = "google-west-memphis-ar"


def memphis() -> dict[str, Any]:
    doc = json.loads(FIX7.read_text(encoding="utf-8"))
    found: dict[str, Any] = next(p for p in doc["projects"] if p["id"] == MEMPHIS)
    return found


@pytest.fixture
def fix7(make_test_context: MakeContext) -> ImportResult:
    args = argparse.Namespace(without_epoch=True, overrides=NO_ENTRIES)
    return IMPORTER.run(make_test_context(input_path=FIX7, now=NOW, today=TODAY), args)


def items(result: ImportResult, pid: str, kind: str | None = None) -> list[ReviewItem]:
    return [i for i in result.review if i.external_id == pid and kind in (None, i.kind)]


def test_every_record_validates(fix7: ImportResult, counties: CountyIndex) -> None:
    for c in fix7.candidates:
        assert validate_record(c.record, counties=counties, today=TODAY) == [], c.match_values


# ---------------------------------------------------------------------------- under construction


def test_a_site_under_construction_holds_a_stage_behind_it(fix7: ImportResult) -> None:
    """Google West Memphis is In review (proposed), but its log's 2026-02-26 entry says the data
    center "remained under construction" and its note says "Under construction." (round 6
    sample, gwa-01m4tac4myb0wtbjcmm85k34y6, published proposed). The entry dates no start of
    construction: the item says it is an observation of that day."""
    assert not fix7.candidates
    (item,) = items(fix7, MEMPHIS, "conflict")
    assert item.reason.startswith(
        "AI GridWatch's stage 'In review' (proposed) is behind its own event log, which reports "
        "the site under construction on 2026-02-26 (event kind 'coverage'), an observation that "
        "dates no start of construction: the row is held for review"
    )
    assert item.data["reported_status"] == "under_construction"
    assert item.data["event_date"] == "2026-02-26"
    assert item.data["observation"] is True


def site(summary: str, *, note: str = "", stage: str = "In review") -> dict[str, Any]:
    """A row with one entry of 2026-02-26."""
    return {
        "id": "example",
        "name": "Example Campus",
        "operator": "Example",
        "locality": "Example",
        "state": "AR",
        "stage": stage,
        "note": note,
        "events": [
            {
                "date": "2026-02-26",
                "kind": "coverage",
                "summary": summary,
                "source": "https://x.test/a",
            }
        ],
    }


@pytest.mark.parametrize(
    "summary",
    [
        "Hiring began at the data center, which remained under construction with an expected "
        "18-24 month build timeline.",
        "KCUR named the LLC behind the data center, noting the facility was under construction.",
        "The data center remains under-construction.",
    ],
)
def test_under_construction_phrases(summary: str) -> None:
    found = log_milestone(site(summary), "proposed", TODAY)
    assert found is not None and found.status == "under_construction"
    assert (found.what, found.observed) == ("the site under construction", True)
    assert found.day is not None and found.day.fuzzy()["value"] == "2026-02-26"


@pytest.mark.parametrize(
    "summary",
    [
        "The data center will be under construction by 2027.",
        "The campus is expected to be under construction next year.",
        "The data center is not yet under construction.",
        # Another site, or the area's sites (the seed's Stargate and Salem Township entries).
        "A venture to invest in 10 data centers, with one already under construction in Texas.",
        "The township is encircled by new data center developments, with multiple campuses under "
        "construction or planned in the area.",
        "The warehouse next door is under construction.",  # no data center, campus or the row's
        "The solar farm powering the data center is under construction.",  # the power supply's
    ],
)
def test_no_site_under_construction(summary: str) -> None:
    assert log_milestone(site(summary), "proposed", TODAY) is None


def test_the_rows_own_note_says_it_without_a_subject() -> None:
    """A clause of the row's own note that starts with it ("Under construction.") is about the
    row; in an entry, or later in the clause, it needs the site named."""
    found = log_milestone(
        site("Coverage.", note="A $4B campus. Under construction."), "proposed", TODAY
    )
    assert found is not None and found.day is None and found.observed
    assert log_milestone(site("Under construction."), "proposed", TODAY) is None
    assert (
        log_milestone(site("Coverage.", note="Phase 1 under construction."), "proposed", TODAY)
        is None
    )


def test_a_groundbreaking_is_no_observation() -> None:
    found = log_milestone(site("Google broke ground on the data center."), "proposed", TODAY)
    assert found is not None and (found.what, found.observed) == (
        "a groundbreaking or construction under way",
        False,
    )


def test_a_stage_at_or_past_construction_is_not_behind() -> None:
    row = site("The data center remained under construction.", stage="Under construction")
    assert log_milestone(row, "under_construction", TODAY) is None
    assert log_milestone(row, "operating", TODAY) is None


# ---------------------------------------------------------------------------- an LLC tied to the operator


VOTE = (
    "West Memphis City Council unanimously approved a MOU, development agreement, and PILOT "
    "incentive package with Groot LLC for a project valued at up to $10 billion, with potential "
    "for up to $50 billion in personal-property tax abatements over 30 years."
)


def test_an_llc_the_row_ties_to_its_operator_names_the_project() -> None:
    """West Memphis's 2025-05-15 council vote names only Groot LLC, which the row's entries tie to
    Google ("Google (via Groot LLC)", "Groot LLC, a Google-linked entity"): it is the first report,
    not the PSC filing of 2025-09 that names Google (round 6 sample,
    gwa-01m4tac4myb0wtbjcmm85k34y6). The land purchase of 2024-11-01 stays a land deal."""
    row = memphis()
    assert tied_llcs(row) == ["Groot LLC"]
    assert names_project(row, VOTE) == "org_place"
    found = first_report(row, "proposed", milestone_events(row, TODAY), TODAY)
    assert found.event is not None
    assert found.event["as_of"] == {"value": "2025-05-15", "precision": "day"}
    assert found.event["status"] == "announced"
    assert found.event["note"] == "Earliest report in AI GridWatch's event log (vote)"


def test_without_the_tie_the_llc_names_no_project() -> None:
    row = memphis()
    for e in row["events"]:
        e["summary"] = (
            e["summary"]
            .replace(" (via Groot LLC)", "")
            .replace("Groot LLC, a Google-linked entity,", "Groot LLC")
        )
    assert tied_llcs(row) == []
    assert names_project(row, VOTE) is None
    found = first_report(row, "proposed", milestone_events(row, TODAY), TODAY)
    assert found.event is not None and found.event["as_of"]["value"] == "2025-09"


def tied(summary: str, operator: str = "Google") -> list[str]:
    row = {"operator": operator, "events": [{"date": "2025-01-01", "summary": summary}]}
    return tied_llcs(row)


@pytest.mark.parametrize(
    ("summary", "operator", "expected"),
    [
        ("Reporting confirmed Google (via Groot LLC) as the company.", "Google", ["Groot LLC"]),
        (
            "The council authorized an agreement with Google (via subsidiary Design LLC).",
            "Google",
            ["Design LLC"],
        ),
        ("Groot LLC, a Google-linked entity, paid $16.5 million.", "Google", ["Groot LLC"]),
        # Another organization's LLC.
        ("Reporting confirmed Microsoft (via Groot LLC) as the company.", "Google", []),
        ("Groot LLC, a Meta-linked entity, paid $16.5 million.", "Google", []),
        # The rejected wider forms: an affiliate or subsidiary may be another site's or another
        # purpose's (Magellan Enterprises LLC, Columbus's, in Google Lancaster's row; Dotier LLC,
        # Meta's solar financier, in Meta Montgomery's).
        ("A related Google subsidiary (Magellan Enterprises LLC) received a CRA.", "Google", []),
        ("Solar projects financed by Meta subsidiary Dotier LLC.", "Meta", []),
    ],
)
def test_tied_llcs(summary: str, operator: str, expected: list[str]) -> None:
    assert tied(summary, operator) == expected
