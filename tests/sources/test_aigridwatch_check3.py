"""AI GridWatch rows of the fourth seed import (2026-10-09) that its record checks found wrong.

tests/fixtures/aigridwatch/check3-2026-10-08.json holds those rows (README.md there says what was
cut). Each test names the bug of the fifth fix round it guards (n0 to n42). The rows are read on
2026-10-09, the day of the seed import, with the check3 samples of the Census place polygons and
county subdivisions.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.geo.places import PlaceIndex
from atlas.geocode import Gazetteer
from atlas.schema.record import FacilityRecord
from atlas.sources import aigridwatch
from atlas.sources.aigridwatch import (
    IMPORTER,
    cousub_areas,
    has_filing,
    milestone_events,
    no_application,
    own_filing,
    parse_locality,
)
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.validate import validate_record

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch"
CHECK3 = SAMPLES / "check3-2026-10-08.json"
COUSUBS = SAMPLES / "gazetteer" / "agw_cousubs_check3.zip"
NOW = datetime(2026, 10, 9, 9, 52, 35, tzinfo=UTC)
TODAY = NOW.date()
MakeContext = Callable[..., ImportContext]


def load() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(CHECK3.read_text(encoding="utf-8"))
    return doc


def row(pid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(p for p in load()["projects"] if p["id"] == pid)
    return found


@pytest.fixture(scope="module")
def check3_places() -> Iterator[PlaceIndex]:
    index = PlaceIndex.load(SAMPLES / "places" / "agw_places_check3.zip", verify_sha256=False)
    yield index
    index.close()


@pytest.fixture
def make_context(
    make_test_context: MakeContext, check3_places: PlaceIndex, monkeypatch: pytest.MonkeyPatch
) -> MakeContext:
    """The shared factory on the import's day, with the check3 samples, and the sample's
    county-subdivision areas in place of the Census file's."""
    gazetteer = Gazetteer.load(cousub_zip=COUSUBS, verify_sha256=False)
    areas = cousub_areas(COUSUBS)
    monkeypatch.setattr(aigridwatch, "_load_cousub_areas", lambda ctx: areas)

    def factory(**overrides: Any) -> ImportContext:
        overrides.setdefault("places", check3_places)
        overrides.setdefault("gazetteer", gazetteer)
        overrides.setdefault("now", NOW)
        overrides.setdefault("today", TODAY)
        return make_test_context(**overrides)

    return factory


def run(
    make_context: MakeContext,
    *,
    overrides: Path | None = None,
    path: Path = CHECK3,
) -> ImportResult:
    args = argparse.Namespace(without_epoch=True)
    if overrides is not None:
        args.overrides = overrides
    return IMPORTER.run(make_context(input_path=path), args)


def run_edited(
    make_context: MakeContext, tmp_path: Path, edit: Callable[[dict[str, Any]], None]
) -> ImportResult:
    doc = load()
    edit(doc)
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return run(make_context, path=path)


@pytest.fixture
def check3(make_context: MakeContext) -> ImportResult:
    return run(make_context)


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    return {c.match_values[0]: c.record for c in result.candidates}


def items(result: ImportResult, pid: str, kind: str | None = None) -> list[ReviewItem]:
    return [i for i in result.review if i.external_id == pid and kind in (None, i.kind)]


def dates(rec: FacilityRecord) -> dict[str, str]:
    return {k: v.value for k, v in rec.dates.items()}


def events(rec: FacilityRecord) -> list[tuple[str, str, str]]:
    return [(e.event, e.status, e.as_of.value) for e in rec.status_history]


def held(result: ImportResult, pid: str) -> list[str]:
    """The reasons of the conflict items that hold a row (it has no record)."""
    assert pid not in records(result), pid
    return [i.reason for i in items(result, pid, "conflict") if "held for review" in i.reason]


def test_every_record_of_the_rows_is_valid(check3: ImportResult, counties: CountyIndex) -> None:
    for pid, rec in records(check3).items():
        assert validate_record(rec, counties=counties, today=TODAY) == [], pid


# ---------------------------------------------------------------------------- n0, n5, n32: places


@pytest.mark.parametrize(
    ("pid", "county", "phrase"),
    [
        ("yerington-monarch-data-center", "Lyon County", "north of Yerington"),
        ("amazon-fort-stockton-pecos", "Pecos County", "11 miles north of Fort Stockton"),
        ("socorro-green-data-nm-tech", "Socorro County", "outside Socorro"),
        ("platte-county-wy-site-layer-4", "Platte County", "northeast of Wheatland"),
        ("tonganoxie-project-bluestem", "Leavenworth County", "south of Tonganoxie"),
    ],
)
def test_a_town_the_row_puts_the_site_outside_is_not_named(
    check3: ImportResult, pid: str, county: str, phrase: str
) -> None:
    # The point is the town's own (it lies in the Census place), but the row's note puts the site
    # outside it: the record names the county, and the point tells only the county (n0, n5, n32).
    rec = records(check3)[pid]
    assert rec.location.city is None and rec.location.municipality is None
    assert rec.location.precision == "county"
    assert rec.canonical_name.endswith(f"({county}, {rec.location.state_abbr})")
    (item,) = [i for i in items(check3, pid, "county_mismatch") if "outside" in i.reason]
    assert item.data["phrase"] == phrase


def test_an_annexation_into_the_town_puts_the_site_outside_it(check3: ImportResult) -> None:
    # Burgin has no coordinates and was placed at the town's Gazetteer point; the developer "sought
    # annexation into City of Burgin", so the farmland is outside it (n32).
    burgin = records(check3)["panattoni-burgin-ky"]
    assert (burgin.location.city, burgin.location.county_name) == (None, "Mercer")
    assert burgin.canonical_name == "Project Bluegrass (Panattoni) (Mercer County, KY)"


def test_a_place_the_row_puts_the_site_in_is_still_named(check3: ImportResult) -> None:
    # Red Oak's council rezoned the land and the point lies in the city: nothing puts it outside.
    assert records(check3)["red-oak-compass-campus"].location.city == "Red Oak"
    # "outside the Sulphur Springs City Council meeting" is about a meeting, not the site.
    assert (
        aigridwatch.outside_phrase(
            {"note": "Residents protest outside the Sulphur Springs City Council meeting."},
            "Sulphur Springs",
        )
        is None
    )
    assert (
        aigridwatch.outside_phrase({"note": "a press event outside Lansing town hall"}, "Lansing")
        is None
    )


# ---------------------------------------------------------------------------- n1, n42: counties


def test_a_row_without_coordinates_is_placed_in_the_county_its_log_states(
    check3: ImportResult, counties: CountyIndex
) -> None:
    # "Fredericksburg" is a market name: VA4 is "on 82 acres in Stafford County near
    # Fredericksburg", and Fredericksburg city is not in Stafford County (n1).
    va4 = records(check3)["vantage-va4-fredericksburg-va"]
    stafford = counties.by_name("VA", "Stafford County")
    assert stafford is not None
    assert (va4.location.county_fips, va4.location.city, va4.location.precision) == (
        stafford.fips,
        None,
        "county",
    )
    assert va4.canonical_name == "Vantage VA4 Fredericksburg (Stafford County, VA)"
    (item,) = [
        i
        for i in items(check3, "vantage-va4-fredericksburg-va", "county_mismatch")
        if "stated_county" in i.data
    ]
    assert item.data["stated_county"] == stafford.fips


def test_a_virginia_independent_city_is_its_own_county(
    make_context: MakeContext, tmp_path: Path
) -> None:
    # Without the log's county, the row is placed at Fredericksburg city, which is its own
    # county-equivalent (51630): the record names it rather than no county (n1).
    def no_log(doc: dict[str, Any]) -> None:
        for p in doc["projects"]:
            if p["id"] == "vantage-va4-fredericksburg-va":
                p["events"] = []

    va4 = records(run_edited(make_context, tmp_path, no_log))["vantage-va4-fredericksburg-va"]
    assert (va4.location.city, va4.location.county_fips) == ("Fredericksburg", "51630")
    assert va4.location.precision == "locality"


def test_a_county_inside_a_compound_parenthesis_is_read(check3: ImportResult) -> None:
    # "Berry Hill (Pittsylvania County / Danville)" names Pittsylvania County (n42).
    loc = parse_locality("Berry Hill (Pittsylvania County / Danville)", "VA")
    assert (loc.place, loc.county_texts, loc.hint) == (
        "Berry Hill",
        ("Pittsylvania County",),
        "Danville",
    )
    stack = records(check3)["stack-berry-hill-pittsylvania-va"]
    assert (stack.location.county_name, stack.location.county_fips) == ("Pittsylvania", "51143")
    assert (
        stack.canonical_name == "STACK Infrastructure Berry Hill megasite (Pittsylvania County, VA)"
    )


# ---------------------------------------------------------------------------- n2, n36: size


def test_it_load_is_not_set_when_the_log_gives_the_campus_other_it_figures(
    check3: ImportResult,
) -> None:
    # The note's "up to 300MW critical IT" against the 2024-09-10 announcement's "240MW of critical
    # IT power" of a 480 MW campus: only mw_as_stated keeps the figure (n2).
    databank = records(check3)["databank-red-oak-tx"]
    assert databank.capacity.it_mw is None
    assert databank.capacity.mw_as_stated == "AI GridWatch size_mw: 300"
    (item,) = [i for i in items(check3, "databank-red-oak-tx", "unit_parse") if "others" in i.data]
    assert item.data["others"] == ["2024-09-10: 240 MW"]


def test_a_named_campus_gets_its_own_acreage(check3: ImportResult) -> None:
    # "the 304-acre BCG Cedar Creek Campus portion of the 2,842-acre development" (n36).
    bcg = records(check3)["bastrop-bcg-cedar-creek-campus"]
    assert bcg.site.acreage == 304
    (item,) = [
        i
        for i in items(check3, "bastrop-bcg-cedar-creek-campus", "unit_parse")
        if "campus_acres" in i.data
    ]
    assert (item.data["acres"], item.data["campus_acres"]) == ("2842", "304")


# ---------------------------------------------------------------------------- n10: placeholders


def test_placeholder_links_date_nothing(check3: ImportResult) -> None:
    # Nine of Drox Rural Hall's ten entries cite ".../article_example.html": its 2026-03-12
    # filing no longer dates the first report, and is no filing (n10).
    drox_row = row("drox-rural-hall-nc")
    filing = next(e for e in drox_row["events"] if e["date"] == "2026-03-12")
    assert not own_filing(filing) and not has_filing(drox_row)
    drox = records(check3)["drox-rural-hall-nc"]
    assert "first_reported" not in drox.dates
    assert [e.event for e in drox.status_history] == ["other"]
    (item,) = items(check3, "drox-rural-hall-nc", "unverified_upstream")
    entries = item.data["entries"]
    assert isinstance(entries, list) and len(entries) == 9 and "2026-03-12" in entries


# ---------------------------------------------------------------------------- n15, n30, n40: filings


def test_an_ordinance_as_context_does_not_unmake_the_rows_own_filing(
    check3: ImportResult,
) -> None:
    # Pronghorn "submitted a conditional use permit application in November 2025 ..., months
    # after the county amended its zoning ordinance": its own filing, so the first report is
    # proposed (n15).
    entry = next(
        e for e in row("antelope-data-campus-iron-county-ut")["events"] if e["date"] == "2025-11-01"
    )
    assert own_filing(entry)
    antelope = records(check3)["antelope-data-campus-iron-county-ut"]
    assert events(antelope)[0] == ("first_reported", "proposed", "2025-11")
    # The county's own rule stays a rule: an entry about the ordinance is no filing.
    assert not own_filing(
        {
            "kind": "rezoning",
            "summary": "The county amended its zoning ordinance to allow data "
            "centers, before the developer submitted its application.",
        }
    )


def test_no_permit_applications_before_a_date_is_no_application(check3: ImportResult) -> None:
    # Project Zora: "with no permit applications before January 2027", and its 2026-08-10 entry
    # "no formal application or site review had been submitted": In review is announced (n30).
    zora = row("project-zora-warrick-county-in")
    assert no_application(zora) == "no permit applications before"
    assert (
        no_application(
            {
                "note": "",
                "events": [
                    {
                        "date": "2026-08-10",
                        "kind": "hearing",
                        "summary": "Residents pushed back against "
                        "Project Zora; no formal application or site review had been submitted.",
                        "source": "",
                    }
                ],
                "name": "Project Zora",
            }
        )
        is not None
    )
    (stage,) = [
        i
        for i in items(check3, "project-zora-warrick-county-in", "conflict")
        if "implies an application" in i.reason
    ]
    assert stage.data["note_says"] == "no permit applications before"


def test_an_inquiry_is_no_filing(check3: ImportResult) -> None:
    # Abei Energy "emailed the Starke County Plan Commission asking about rezoning two parcels";
    # its rezoning_filed is the day the inquiry was disclosed (n40).
    abei_row = row("abei-energy-data-center-starke-in")
    assert aigridwatch.rezoning_filing(abei_row).day is None and not has_filing(abei_row)
    abei = records(check3)["abei-energy-data-center-starke-in"]
    assert "application_filed" not in abei.dates
    assert [e.event for e in abei.status_history] == ["announced", "other"]
    assert abei.status == "paused"
    (item,) = [
        i
        for i in items(check3, "abei-energy-data-center-starke-in", "conflict")
        if "rezoning_filed" in i.data
    ]
    assert "inquiry" in item.reason and item.data["application_filed"] is None


# ---------------------------------------------------------------------------- n31, n33: dates


def test_an_announced_date_the_row_calls_a_registration_is_no_announcement(
    check3: ImportResult,
) -> None:
    # Andover: "National Land Developers registered Andover HPC Development in December 2025"
    # (n31). The row is held for its township-wide ban (n34).
    explained = aigridwatch.announcement(row("andover-twp-stickles-pond"))
    assert explained.day is None and explained.filing is None
    assert explained.doubt is not None and "registration" in explained.doubt
    assert any("announced date" in i.reason for i in items(check3, "andover-twp-stickles-pond"))


def test_an_announced_date_the_row_calls_a_filing_is_the_filing(check3: ImportResult) -> None:
    # Muncy: "filed a zoning permit application on April 15, 2026, and a conditional use
    # application on April 28": April 15 is the application's day, not an announcement (n31,
    # n33).
    muncy = row("muncy-twp-fishlips")
    assert aigridwatch.announcement(muncy).filing == aigridwatch.AgwDate(date(2026, 4, 15), "day")
    assert [(e["event"], e["as_of"]["value"]) for e in milestone_events(muncy, TODAY)] == [
        ("application_filed", "2026-04-15")
    ]
    # It is held: supervisors "impose nine-month moratorium on data center submissions" after
    # the filing (n35).
    assert any("moratorium" in r for r in held(check3, "muncy-twp-fishlips"))


def test_rezoning_filed_is_checked_against_the_rows_own_filings(check3: ImportResult) -> None:
    # Smithfield's rezoning_filed is the township's curative amendment resolution ("180-day MPC
    # review period begins"), not the developer's application (n33).
    smithfield = records(check3)["smithfield-gateway-data-center"]
    assert "application_filed" not in smithfield.dates
    assert smithfield.status == "proposed"  # the developer's own filings
    # Monticello Tech's 2026-07-20 entry names its "July 6 land-use applications" (n33).
    assert dates(records(check3)["monticello-tech-monticello-mn"])["application_filed"] == (
        "2026-07-06"
    )
    # Site Layer 4's rezoning_filed is the day the application was "revealed" (n33).
    layer4 = records(check3)["platte-county-wy-site-layer-4"]
    assert "application_filed" not in layer4.dates and layer4.status == "proposed"
    reasons = [i.reason for i in items(check3, "platte-county-wy-site-layer-4", "conflict")]
    assert any("only reports the application" in r for r in reasons)


def test_an_environmental_review_of_the_site_reports_the_project(check3: ImportResult) -> None:
    # The 2025-11-01 Draft AUAR "for a 550-acre industrial area" (the row's 547 acres) is the
    # first report, not the AUAR's adoption of 2026-01-26 (n38).
    monticello = records(check3)["monticello-tech-monticello-mn"]
    assert dates(monticello)["first_reported"] == "2025-11"


def test_a_vote_around_midnight_is_dated_by_its_meeting(check3: ImportResult) -> None:
    # "approved the rezoning 4-1 around midnight following the May 11 meeting" (n37).
    decision = aigridwatch.decision_day(row("red-oak-compass-campus"))
    assert decision.day == aigridwatch.AgwDate(date(2026, 5, 11), "day")
    red_oak = records(check3)["red-oak-compass-campus"]
    assert dates(red_oak)["approved"] == "2026-05-11"
    assert ("hearing_held", "proposed", "2026-05-11") in events(red_oak)


# ---------------------------------------------------------------------------- n34, n35, n39: holds


def test_an_injunction_holds_a_row_under_construction(check3: ImportResult) -> None:
    # Matrix: the judge "issues a temporary injunction freezing new construction/development on
    # the ~5,000-acre site pending trial" (n34).
    reasons = held(check3, "msb-sulphur-springs-tx")
    assert any("injunction or a halt" in r for r in reasons)
    # And its developer is "being phased out": not a party the record would publish.
    replaced = [
        i for i in items(check3, "msb-sulphur-springs-tx", "conflict") if "replaced" in i.reason
    ]
    assert replaced and replaced[0].data["operator"] == "MSB Global Services"
    # The developer taking over is not the one replaced; a refused injunction halts nothing.
    swap = {"note": "CyrusOne replaced MSB Global as developer of the campus."}
    assert aigridwatch.party_replaced(swap, "CyrusOne") is None
    assert aigridwatch.party_replaced(swap, "MSB Global Services") is not None
    refused = {
        "note": "",
        "name": "Example Campus",
        "events": [
            {
                "date": "2026-02-10",
                "kind": "ruling",
                "summary": "The judge refused to grant an injunction halting construction at the "
                "site.",
                "source": "",
            }
        ],
    }
    assert aigridwatch.log_obstacle(refused, "under_construction", TODAY) is None


def test_a_ban_holds_an_announced_row_without_a_filing(check3: ImportResult) -> None:
    # Andover's note: "the township passed Ordinance 2026-13 banning data centers township-wide"
    # (n34).
    assert any("ban" in r for r in held(check3, "andover-twp-stickles-pond"))
    # Project Zora: commissioners "voted to enact a 180-day moratorium" after the project was
    # public (n34).
    assert any("moratorium" in r for r in held(check3, "project-zora-warrick-county-in"))
    # Leavenworth County's moratorium expired before the file's date: Project Bluestem stays.
    assert records(check3)["tonganoxie-project-bluestem"].status == "announced"


def test_present_tense_impose_is_a_moratorium() -> None:
    # "Township supervisors impose nine-month moratorium on data center submissions" (n35).
    assert aigridwatch._LOG_MORATORIUM_RE.search(
        "Township supervisors impose nine-month moratorium on data center submissions."
    )


def test_a_groundbreaking_by_the_company_in_the_note_holds_the_row(check3: ImportResult) -> None:
    # Nebius: "Council approved Chapter 100 abatement 5-2 and the company has broken ground" (n39).
    assert any("groundbreaking" in r for r in held(check3, "nebius-independence-mo"))


# ---------------------------------------------------------------------------- n41: first reports


@pytest.mark.parametrize(
    ("pid", "event"),
    [("project-riverjump-marion-in", "withdrawn"), ("douglas-county-i20-ga", "denied")],
)
def test_a_terminal_decision_never_dates_the_first_report(
    check3: ImportResult, pid: str, event: str
) -> None:
    # Riverjump's only milestone is the mayor's withdrawal, Douglas County's the denial: the
    # project was public before, so the row is held for its first report (n41).
    (reason,) = held(check3, pid)
    assert f"its {event} event" in reason


def test_a_reviewer_dates_or_releases_a_terminal_first_report(
    make_context: MakeContext, tmp_path: Path
) -> None:
    overrides = tmp_path / "aigridwatch.json"
    overrides.write_text(
        json.dumps(
            {
                "project-riverjump-marion-in": {
                    "first_reported": "2026-08-23",
                    "reason": "test: a report dated by its source",
                    "reviewed_at": "2026-10-09",
                },
                "douglas-county-i20-ga": {
                    "release": ["first_report"],
                    "reason": "test: the denial accepted as the first report",
                    "reviewed_at": "2026-10-09",
                },
            }
        ),
        encoding="utf-8",
    )
    got = records(run(make_context, overrides=overrides))
    assert dates(got["project-riverjump-marion-in"])["first_reported"] == "2026-08-23"
    assert dates(got["douglas-county-i20-ga"])["first_reported"] == "2026-07-08"
