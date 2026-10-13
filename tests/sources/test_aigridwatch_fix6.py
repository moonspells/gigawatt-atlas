"""AI GridWatch rules of fix round 6, each measured on the seed's inputs against the review of
the fifth seed (the owner's correction a rule follows is named by its record id).

tests/fixtures/aigridwatch/fix6-2026-10-08.json holds the rows (README.md there says what was
cut); the negative cases also read rows of the earlier fixtures. The rows are read on
2026-10-09, the day of the seed import.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.schema.record import FacilityRecord
from atlas.sources.aigridwatch import (
    IMPORTER,
    AgwDate,
    log_obstacle,
    milestone_events,
    property_deal,
    stage_ahead,
)
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.validate import validate_record

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch"
FIX6 = SAMPLES / "fix6-2026-10-08.json"
NOW = datetime(2026, 10, 9, 9, 52, 35, tzinfo=UTC)
TODAY = NOW.date()
MakeContext = Callable[..., ImportContext]


def row(pid: str, path: Path = FIX6) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    found: dict[str, Any] = next(p for p in doc["projects"] if p["id"] == pid)
    return found


@pytest.fixture
def fix6(make_test_context: MakeContext) -> ImportResult:
    args = argparse.Namespace(without_epoch=True)
    return IMPORTER.run(make_test_context(input_path=FIX6, now=NOW, today=TODAY), args)


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    return {c.match_values[0]: c.record for c in result.candidates}


def items(result: ImportResult, pid: str, kind: str | None = None) -> list[ReviewItem]:
    return [i for i in result.review if i.external_id == pid and kind in (None, i.kind)]


def test_every_record_validates(fix6: ImportResult, counties: CountyIndex) -> None:
    for c in fix6.candidates:
        assert validate_record(c.record, counties=counties, today=TODAY) == [], c.match_values


# ---------------------------------------------------------------------------- G1, G2: bans


NO_FILING = (
    "implies an application under review, but its own event log reports a moratorium or ban on "
    "data centers adopted after the project was public on {day} (event kind {kind!r}), and the "
    "row shows no filing: the row is held for review"
)


def test_a_moratorium_on_an_application_with_no_filing_holds_the_row(fix6: ImportResult) -> None:
    """Sarasota County's row is In review with no filing, and its log reports the commission's
    'voted 5-0 for 1-year moratorium' (owner's correction gwa-01m4hfr0fdd7r51zqa7vk5yqmx);
    Tulare County's 45-day moratorium, Inver Grove Heights' 'Council voted 3-2 for 1-year
    moratorium', and QTS Dane County's withdrawal before Madison's and Dane County's moratoria
    are the same case."""
    got = records(fix6)
    for pid, day, kind in (
        ("sarasota-county-moratorium-fl", "2026-07-09", "vote"),
        ("tulare-county-moratorium-ca", "2026-08", "vote"),
        ("inver-grove-heights-mn", "2026-06-26", "vote"),
        ("qts-dane-county-wi", "2026-01-13", "moratorium"),
    ):
        assert pid not in got
        (item,) = items(fix6, pid, "conflict")
        assert NO_FILING.format(day=day, kind=kind) in item.reason, pid
        assert (item.data["reported_status"], item.data["stage"]) == ("paused", "In review")


def test_an_application_stage_without_a_ban_is_still_proposed(fix6: ImportResult) -> None:
    """Kearney's Awaiting decision row has no filing and no ban: it stays proposed, as 9 of the
    10 reviewed such rows were judged right."""
    assert records(fix6)["kearney-data-center-ne"].status == "proposed"


def vote(summary: str, day: str = "2026-06-26") -> dict[str, Any]:
    return {
        "id": "example",
        "stage": "In review",
        "note": "",
        "events": [{"date": day, "kind": "vote", "summary": summary, "source": "https://x.test/a"}],
    }


@pytest.mark.parametrize(
    "summary",
    [
        "Council voted 3-2 for 1-year moratorium.",
        "Commission voted unanimously in favor of a moratorium on data centers.",
        "The county responded with unanimous 1-year moratorium (5-0).",
        "The council approved a rewritten zoning code that permanently excludes data centers from "
        "the city.",
    ],
)
def test_ban_phrases(summary: str) -> None:
    found = log_obstacle(vote(summary), "proposed", TODAY, public=date(2026, 1, 1))
    assert found is not None and found.status == "paused"
    assert found.detail == ", and the row shows no filing"


@pytest.mark.parametrize(
    ("summary", "public"),
    [
        # A demand or a draft is no ban.
        (
            "Residents asked the council to adopt a code that excludes data centers from the city.",
            None,
        ),
        ("Residents urged the board, which voted for a moratorium study, to act.", None),
        # A ban adopted before the project was public does not block it.
        ("Council voted 3-2 for 1-year moratorium.", date(2026, 7, 1)),
    ],
)
def test_no_ban(summary: str, public: date | None) -> None:
    assert log_obstacle(vote(summary), "proposed", TODAY, public=public) is None


def test_a_ban_after_its_filing_holds_a_filed_row(fix6: ImportResult) -> None:
    """T5's Palmetto row: 'approved a rewritten industrial zoning code that permanently excludes
    data centers from the city', after its DRI filing (owner's correction
    gwa-01m4hfr0cfg6ehm4zb2m0hbjg8)."""
    assert "t5-palmetto-ga" not in records(fix6)
    (item,) = items(fix6, "t5-palmetto-ga", "conflict")
    assert "a moratorium or ban on data centers adopted after its filing on 2026-03" in item.reason
    assert "no filing" not in item.reason


# ---------------------------------------------------------------------------- G3b, G4b: ahead


def test_operation_entries_of_an_older_site_do_not_make_a_plan_operating(
    fix6: ImportResult,
) -> None:
    """Microsoft Cheyenne Expansion's stage is Operating, its operational entries (2012, 2021)
    are the existing campus's, and its note speaks only of the 3,200-acre plan announced on
    2026-04-14 (owner's correction gwa-01m4hfr0dkjxxqhcjzqfqh4pcy)."""
    assert "microsoft-cheyenne-expansion" not in records(fix6)
    (item,) = items(fix6, "microsoft-cheyenne-expansion", "conflict")
    assert (
        "stage 'Operating' (operating) is ahead of its own row: its operational entries all come "
        "before the project was announced (2026-04-14; the latest is of 2021-12-31)"
    ) in item.reason
    assert "held for review" in item.reason


def test_a_row_whose_note_reports_a_site_built_is_not_ahead() -> None:
    """The fixture's operating google-bristow has later announcements (investment news), and a
    note that says the site operates; and the Cheyenne row with a note that says so is not
    held either."""
    bristow = row("google-bristow", SAMPLES / "projects.json")
    assert stage_ahead(bristow, "operating", TODAY) is None
    cheyenne = row("microsoft-cheyenne-expansion")
    assert stage_ahead(cheyenne, "operating", TODAY) is not None
    cheyenne["note"] = "Microsoft operates three data centers in Cheyenne and plans an expansion."
    assert stage_ahead(cheyenne, "operating", TODAY) is None


def test_an_incentive_is_no_approval_to_build() -> None:
    """STACK Berry Hill's Approved stage rests on a performance agreement (owner's correction
    gwa-01m4hfr0pzx7pp6mt8ptap6rm7); the importer run is in test_aigridwatch_check3.py."""
    berry = row("stack-berry-hill-pittsylvania-va", SAMPLES / "check3-2026-10-08.json")
    why = stage_ahead(berry, "permitted", TODAY)
    assert why is not None and "an incentive, not an approval to build" in why


@pytest.mark.parametrize(
    ("pid", "path"),
    [
        ("dc-blox-warren-township-in", "seed-2026-10-08.json"),
        ("edgecore-louisa-county-va", "seed-2026-10-08.json"),
        ("flexential-kaufman-tx", "fix6-2026-10-08.json"),
        ("google-botetourt-greenfield-va", "check2-2026-10-08.json"),  # its grading permit
    ],
)
def test_an_approval_without_an_incentive_or_with_a_permit_stands(pid: str, path: str) -> None:
    """The review judged these Approved rows right as permitted."""
    assert stage_ahead(row(pid, SAMPLES / path), "permitted", TODAY) is None


def test_flexential_kaufman_stays_permitted(fix6: ImportResult) -> None:
    assert records(fix6)["flexential-kaufman-tx"].status == "permitted"


# ---------------------------------------------------------------------------- G6: a land deal


def test_a_land_deal_is_the_first_report_not_the_announcement(fix6: ImportResult) -> None:
    """Amazon Bridgefield's announced date is the day Amazon Data Services 'bought George
    Washington University's Ashburn campus for $427 million' (owner's correction
    gwa-01m4hfr0pmrv4jnpy47nac4m2z)."""
    bridgefield = records(fix6)["amazon-bridgefield-ashburn-va"]
    assert {k: v.value for k, v in bridgefield.dates.items()} == {
        "application_filed": "2026-07-31",
        "first_reported": "2026-03-17",
    }
    assert [(e.event, e.status) for e in bridgefield.status_history] == [
        ("first_reported", "announced"),
        ("application_filed", "proposed"),
    ]
    (item,) = [
        i for i in items(fix6, "amazon-bridgefield-ashburn-va", "conflict") if "deal" in i.reason
    ]
    assert item.data["announced"] == "2026-03-17"
    assert "imported as the project's first report, not as its announcement" in item.reason


def test_an_announced_land_purchase_with_a_plan_stays_an_announcement() -> None:
    """EdgeCore 'announced the land purchase ... and $17B+ phased campus plan'."""
    louisa = row("edgecore-louisa-county-va", SAMPLES / "seed-2026-10-08.json")
    day = AgwDate(date.fromisoformat(louisa["announced"]), "day")
    assert property_deal(louisa, day) is None
    events = milestone_events(louisa, TODAY)
    assert events[0]["event"] == "announced"


# ---------------------------------------------------------------------------- G7: the county


def test_a_place_the_row_puts_the_site_outside_still_gives_its_county(fix6: ImportResult) -> None:
    """CloudBurst 'near San Marcos' has no coordinates and was placed at San Marcos's Gazetteer
    point, which then named neither the town nor the county (owner's correction
    gwa-01m4hfr0bh3vmb3svd314s8636); Applied Digital's campus 'southeast of Harwood' too."""
    got = records(fix6)
    for pid, county, name in (
        ("cloudburst-san-marcos-tx", "Hays", "CloudBurst San Marcos (Hays County, TX)"),
        ("applied-digital-harwood-nd", "Cass", "Applied Digital Harwood ND (Cass County, ND)"),
    ):
        loc = got[pid].location
        assert (loc.precision, loc.city, loc.county_name) == ("county", None, county), pid
        assert got[pid].canonical_name == name
        (item,) = items(fix6, pid, "county_mismatch")
        assert "keeps the point only at county precision" in item.reason


def test_a_gazetteer_place_the_row_does_not_put_the_site_outside_is_named(
    fix6: ImportResult,
) -> None:
    databank = records(fix6)["databank-cartersville-ga"].location
    assert (databank.precision, databank.city, databank.county_name) == (
        "locality",
        "Cartersville",
        "Bartow",
    )
