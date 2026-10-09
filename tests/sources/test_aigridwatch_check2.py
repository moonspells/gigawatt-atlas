"""AI GridWatch rows of the second seed import (2026-10-09) that the record checks found wrong.

tests/fixtures/aigridwatch/check2-2026-10-08.json holds those rows (README.md there says what was
cut). Each test names the bug of the fourth fix round it guards (n24 to n45). The rows are read
on 2026-10-09, the day of the seed import, with the county subdivisions' areas of the sample, so
the township extent test runs.
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
    first_report,
    milestone_events,
    own_filing,
    parse_agw_date,
    place_parts,
    source_date,
)
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.validate import validate_record

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch"
CHECK2 = SAMPLES / "check2-2026-10-08.json"
COUSUBS = SAMPLES / "gazetteer" / "agw_cousubs.zip"
NOW = datetime(2026, 10, 9, 9, 52, 35, tzinfo=UTC)
TODAY = NOW.date()
MakeContext = Callable[..., ImportContext]


def load() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(CHECK2.read_text(encoding="utf-8"))
    return doc


def row(pid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(p for p in load()["projects"] if p["id"] == pid)
    return found


@pytest.fixture(scope="module")
def agw_places() -> Iterator[PlaceIndex]:
    index = PlaceIndex.load(SAMPLES / "places" / "agw_places.zip", verify_sha256=False)
    yield index
    index.close()


@pytest.fixture
def make_context(
    make_test_context: MakeContext, agw_places: PlaceIndex, monkeypatch: pytest.MonkeyPatch
) -> MakeContext:
    """The shared factory on the import's day, with the AI GridWatch samples, and the sample's
    county-subdivision areas in place of the Census file's."""
    gazetteer = Gazetteer.load(cousub_zip=COUSUBS, verify_sha256=False)
    areas = cousub_areas(COUSUBS)
    monkeypatch.setattr(aigridwatch, "_load_cousub_areas", lambda ctx: areas)

    def factory(**overrides: Any) -> ImportContext:
        overrides.setdefault("places", agw_places)
        overrides.setdefault("gazetteer", gazetteer)
        overrides.setdefault("now", NOW)
        overrides.setdefault("today", TODAY)
        return make_test_context(**overrides)

    return factory


def run(make_context: MakeContext, **ctx: Any) -> ImportResult:
    without_epoch = "records" not in ctx
    args = argparse.Namespace(without_epoch=without_epoch)
    return IMPORTER.run(make_context(input_path=CHECK2, **ctx), args)


@pytest.fixture
def check2(make_context: MakeContext) -> ImportResult:
    return run(make_context)


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    return {c.match_values[0]: c.record for c in result.candidates}


def items(result: ImportResult, pid: str, kind: str | None = None) -> list[ReviewItem]:
    return [i for i in result.review if i.external_id == pid and kind in (None, i.kind)]


def dates(rec: FacilityRecord) -> dict[str, str]:
    return {k: v.value for k, v in rec.dates.items()}


def test_every_record_of_the_rows_is_valid(check2: ImportResult, counties: CountyIndex) -> None:
    for pid, rec in records(check2).items():
        assert validate_record(rec, counties=counties, today=TODAY) == [], pid


# ---------------------------------------------------------------------------- n24: first report


def test_a_land_purchase_does_not_date_the_first_report(check2: ImportResult) -> None:
    # Aligned bought the parcels in "2023" (Baxtel); its site plan, filed in November 2025, is
    # the first report of the data center ('Aligned Data Centers' is a name, not a report).
    pataskala = records(check2)["aligned-pataskala-oh"]
    assert pataskala.dates["first_reported"].model_dump() == {
        "value": "2025-11",
        "precision": "month",
    }
    assert pataskala.status_history[0].status == "proposed"  # the row's own site plan


def test_a_report_on_the_project_that_mentions_a_moratorium_counts(check2: ImportResult) -> None:
    # Posey County: residents met on 2026-06-03 to oppose the data center at Boberg and Hoenert
    # Roads and asked for a moratorium; that report was skipped, so the record said 2026-07-22.
    posey = records(check2)["posey-county-boberg-hoenert-in"]
    assert dates(posey)["first_reported"] == "2026-06-03"


def test_the_operator_with_the_place_names_the_project(check2: ImportResult) -> None:
    # TeraWulf's lease announcement of 2025-08-14 names "the Lansing site", not a data center.
    terawulf = records(check2)["terawulf-cayuga-lansing-ny"]
    assert dates(terawulf)["first_reported"] == "2025-08-14"


def test_another_campus_of_the_same_company_does_not_date_the_first_report(
    check2: ImportResult,
) -> None:
    # Red Oak: the 2020 entry is Compass's first campus (225 acres), the 2023 ones its 375 more
    # acres and that campus's Phase 2; the 830-acre rezoning was first reported at the P&Z vote.
    red_oak = records(check2)["red-oak-compass-campus"]
    assert dates(red_oak)["first_reported"] == "2026-04-27"


def test_the_filing_month_the_row_states_bounds_the_first_report(check2: ImportResult) -> None:
    # Hanover: "filed a conditional-use application in May 2026"; the township's ordinance entry
    # is about the rule itself, and the CBS report of 2026-07-27 is later than the filing.
    hanover = records(check2)["hanover-township-starpointe-pa"]
    assert hanover.dates["first_reported"].model_dump() == {
        "value": "2026-05",
        "precision": "month",
    }
    assert hanover.status_history[0].status == "proposed"


def test_a_row_whose_first_report_cannot_be_told_is_held(check2: ImportResult) -> None:
    # Plaza 500: the 2022 purchase does not date it, and the first entry that names the data
    # center (the SCC's hearing on its substation, November 2024) is dated by the event it
    # announces and its source gives no date. Imported, the record would say first reported on
    # its 2026-07-16 filing, later than its own row shows: held, with the reason.
    assert "plaza-500-lincolnia-va" not in records(check2)
    (item,) = [
        i
        for i in items(check2, "plaza-500-lincolnia-va", "conflict")
        if "first_reported" in i.reason
    ]
    assert item.data["earliest"] == "2024-11" and item.data["derived"] == "2026-07-16"


# ---------------------------------------------------------------------------- n25, n30: years


def test_january_first_is_a_year_unless_the_text_names_january() -> None:
    plaza = row("plaza-500-lincolnia-va")
    sale = next(e for e in plaza["events"] if e["date"] == "2022-01-01")
    assert parse_agw_date(sale["date"], sale["summary"]).fuzzy() == {  # type: ignore[union-attr]
        "value": "2022",
        "precision": "year",
    }
    pataskala = row("aligned-pataskala-oh")
    bought = next(e for e in pataskala["events"] if e["date"] == "2023-01-01")
    assert "year reported as 2023" in bought["summary"]
    assert parse_agw_date(bought["date"], bought["summary"]).precision == "year"  # type: ignore[union-attr]
    # "(reported as November 2025)": the 1st of another month stays that month.
    filed = next(e for e in pataskala["events"] if e["date"] == "2025-11-01")
    assert parse_agw_date(filed["date"], filed["summary"]).precision == "month"  # type: ignore[union-attr]
    assert parse_agw_date("2025-01-01", "Filed in January 2025.").precision == "month"  # type: ignore[union-attr]
    assert parse_agw_date("2024-09-01", "in 2024 the board met").precision == "year"  # type: ignore[union-attr]
    assert parse_agw_date("2026-03-02").precision == "day"  # type: ignore[union-attr]


def test_a_milestone_on_january_first_is_that_year(check2: ImportResult) -> None:
    # Metrobloks' announced and rezoning_filed read 2025-01-01: "In 2025, Metrobloks filed".
    metrobloks = records(check2)["metrobloks-martindale-brightwood-in"]
    assert {k: (v.value, v.precision) for k, v in metrobloks.dates.items()} == {
        "first_reported": ("2025", "year"),
        "announced": ("2025", "year"),
        "application_filed": ("2025", "year"),
        "approved": ("2026-05-04", "day"),
    }


# ---------------------------------------------------------------------------- n26, n31: places


def test_new_york_towns_are_municipalities(check2: ImportResult) -> None:
    got = records(check2)
    # Lansing, NY: the town (whose point AI GridWatch gives), not Lansing village, 9 km away.
    lansing = got["terawulf-cayuga-lansing-ny"].location
    assert (lansing.city, lansing.municipality) == (None, "Lansing")
    # Alabama, NY is a town and no Census place.
    alabama = got["stamp-double-reed-alabama-ny"].location
    assert (alabama.city, alabama.municipality) == (None, "Alabama")


def test_a_township_the_point_is_far_outside_is_not_named(check2: ImportResult) -> None:
    # Hanover Township's point lies in Jefferson Township, 8.8 km from Hanover's own point.
    hanover = records(check2)["hanover-township-starpointe-pa"]
    assert hanover.location.municipality is None
    assert hanover.canonical_name.endswith("(Washington County, PA)")
    (item,) = items(check2, "hanover-township-starpointe-pa", "county_mismatch")
    assert item.data["place"] == "Hanover Township" and "8.8 km" in item.reason


def test_a_township_whose_point_lies_in_a_borough_is_not_named(check2: ImportResult) -> None:
    # Smithfield Gateway's point lies inside East Stroudsburg borough, a municipality of its own.
    smithfield = records(check2)["smithfield-gateway-data-center"]
    assert smithfield.location.municipality is None
    (item,) = items(check2, "smithfield-gateway-data-center", "county_mismatch")
    assert "East Stroudsburg borough" in item.reason


def test_of_a_compound_locality_only_the_place_that_passes_is_named(
    check2: ImportResult,
) -> None:
    assert place_parts("North Beaver & Mahoning townships") == [
        ("North Beaver Township", None),
        ("Mahoning Township", None),
    ]
    assert place_parts("Warren Township (Indianapolis) / Irvington") == [
        ("Warren Township", "Indianapolis"),
        ("Irvington", None),
    ]
    powder = records(check2)["powder-mill-works-lawrence-pa"].location
    assert powder.municipality == "North Beaver Township"


def test_a_neighborhood_gives_way_to_the_city_its_parenthesis_names(
    check2: ImportResult,
) -> None:
    got = records(check2)
    # "Martindale-Brightwood (Indianapolis)": the point is in Indianapolis city (balance).
    assert got["metrobloks-martindale-brightwood-in"].location.city == "Indianapolis"
    # "Globeville-Elyria-Swansea (Denver)", no coordinates: placed at Denver, named Denver.
    assert got["coresite-de3-denver-co"].location.city == "Denver"
    # Wilkes-Barre city contains Avison Young's PA DEP point.
    assert got["avison-young-unnamed-data-center-city-of-wilkes-barre-pa"].location.city == (
        "Wilkes-Barre"
    )


# ---------------------------------------------------------------------------- n27: no application


def test_a_stage_that_implies_an_application_is_announced_when_none_was_made(
    check2: ImportResult,
) -> None:
    # Posey County: "despite no official proposal"; its only 'rezoning' entry is the Area Plan
    # Commission revising its own data center ordinance, which is no filing of the project.
    ordinance = next(
        e for e in row("posey-county-boberg-hoenert-in")["events"] if e["kind"] == "rezoning"
    )
    assert not own_filing(ordinance)
    posey = records(check2)["posey-county-boberg-hoenert-in"]
    assert posey.status == "announced"
    (item,) = [
        i
        for i in items(check2, "posey-county-boberg-hoenert-in", "conflict")
        if "implies an application" in i.reason
    ]
    assert item.data == {"stage": "Awaiting decision", "note_says": "no official proposal"}


# ---------------------------------------------------------------------------- n29: report dates


def test_a_report_is_never_dated_after_its_source() -> None:
    assert source_date("https://www.inkfreenews.com/2026/06/26/white-county-data-center/") == date(
        2026, 6, 26
    )
    assert source_date("https://www.observer-reporter.com/news/2026/aug/07/x/") == date(2026, 8, 7)
    assert source_date("https://www.maysville-online.com/news/214311/hyperscale") is None


def test_an_open_house_is_reported_on_its_sources_day() -> None:
    # Wolcott's open-house entry is dated 2026-06-29, the event's day; InkFreeNews reported it on
    # 2026-06-26. Without the row's earlier hearing, the report is dated by its source.
    wolcott = row("blue-ladder-wolcott-data-center-in")
    wolcott = {**wolcott, "hearing_date": "", "events": [wolcott["events"][-1]]}
    assert wolcott["events"][0]["date"] == "2026-06-29"
    found = first_report(wolcott, "proposed", milestone_events(wolcott, TODAY), TODAY)
    assert found.event is not None
    assert found.event["as_of"] == {"value": "2026-06-26", "precision": "day"}
    assert "dated by its source" in found.event["note"]


def test_an_entry_dated_by_the_event_it_announces_does_not_date_the_report(
    check2: ImportResult,
) -> None:
    # Mason County: "scheduled public hearings for March 25-26" is dated 2026-03-25; its source
    # was published two days before and gives no date. Imported, the record would be first
    # reported on its approval of 2026-05-22: held.
    assert "mason-county-hyperscale-campus" not in records(check2)
    (item,) = [
        i
        for i in items(check2, "mason-county-hyperscale-campus", "conflict")
        if "first_reported" in i.reason
    ]
    assert item.data["event_date"] == "2026-03-25" and item.data["derived"] == "2026-05-22"


def test_an_unconfirmed_hearing_bounds_the_first_report() -> None:
    # Wolcott: the open house of 2026-06-29 (InkFreeNews, 2026-06-26) is ten months after the
    # row's own hearing_date, 2025-08-11, which no event is imported for.
    wolcott = row("blue-ladder-wolcott-data-center-in")
    events = milestone_events(wolcott, TODAY)
    found = first_report(wolcott, "proposed", events, TODAY)
    assert found.event is None
    assert found.earliest is not None and found.earliest.start == date(2025, 8, 11)


# ---------------------------------------------------------------------------- n32: later milestones


def test_construction_the_note_or_the_log_reports_holds_the_row(check2: ImportResult) -> None:
    # Meta Lebanon: "groundbreaking construction reported ongoing" (note) and construction
    # disruption "during Meta campus build-out" (log), under the stage Proposed.
    assert "meta-leap-lebanon-in" not in records(check2)
    (item,) = [i for i in items(check2, "meta-leap-lebanon-in", "conflict") if "behind" in i.reason]
    assert item.data["reported_status"] == "under_construction"


def test_a_refused_application_or_a_ban_after_the_filing_holds_the_row(
    check2: ImportResult,
) -> None:
    # Urbana: the moratorium of 2026-03-03, after the 2026-02-13 filing that the BZA upheld as
    # incomplete, under the stage In review.
    assert "urbana-oh-thor-equities" not in records(check2)
    (item,) = [
        i
        for i in items(check2, "urbana-oh-thor-equities", "conflict")
        if "implies an application under review" in i.reason
    ]
    assert item.data["reported_status"] == "paused" and item.data["event_date"] == "2026-03-03"


def test_an_approval_under_court_challenge_holds_the_row(check2: ImportResult) -> None:
    # Wolcott: neighbors "sue ... to overturn the rezoning": the rezoning was approved.
    assert "blue-ladder-wolcott-data-center-in" not in records(check2)
    (item,) = [
        i
        for i in items(check2, "blue-ladder-wolcott-data-center-in", "conflict")
        if "under court challenge" in i.reason
    ]
    assert item.data["reported_status"] == "permitted"


def test_a_ballot_measure_that_could_ban_is_not_a_ban(check2: ImportResult) -> None:
    # Pataskala's council put a charter amendment that "could ban" large data centers on the
    # ballot: the row stays published.
    assert "aligned-pataskala-oh" in records(check2)


# ---------------------------------------------------------------------------- n34: approvals


def test_an_approval_of_an_incentive_is_no_approval_to_build(check2: ImportResult) -> None:
    # Botetourt's decision of 2025-06-24 approved a performance agreement; the stage Approved
    # stands (the grading permit of August 2026) as an observation that dates nothing.
    botetourt = records(check2)["google-botetourt-greenfield-va"]
    assert botetourt.status == "permitted"
    assert "approved" not in botetourt.dates
    assert [e.event for e in botetourt.status_history] == ["announced", "other"]
    (item,) = [
        i
        for i in items(check2, "google-botetourt-greenfield-va", "conflict")
        if "land-use approval" in i.reason
    ]
    assert item.data["decided_date"] == "2025-06-24"
    assert "performance agreement" in str(item.data["entry"])
    # A rezoning approval stays an approval: Red Oak's council "approved the rezoning".
    assert dates(records(check2)["red-oak-compass-campus"])["approved"] == "2026-05-12"


# ---------------------------------------------------------------------------- n37: roles


def test_the_operator_developer_field_is_the_developer(check2: ImportResult) -> None:
    got = records(check2)
    avison = got["avison-young-unnamed-data-center-city-of-wilkes-barre-pa"].parties
    assert ([o.name for o in avison.developer], avison.operator) == (["Avison Young"], [])
    assert not any(r.parties.operator for r in got.values())


def test_a_utility_in_the_operator_field_is_not_a_party(check2: ImportResult) -> None:
    # PSE&G is named only as the utility whose approval was required.
    pseg = records(check2)["pseg-100-jersey-ave-nj"]
    assert not (pseg.parties.operator or pseg.parties.developer)
    (item,) = items(check2, "pseg-100-jersey-ave-nj", "unit_parse")
    assert item.data == {"operator": "PSE&G"}


# ---------------------------------------------------------------------------- n44, n45: Epoch


def epoch_record(
    make_record: Callable[..., FacilityRecord],
    name: str,
    location: dict[str, Any],
    *,
    tenant: tuple[str, ...] = (),
    links: tuple[str, ...] = (),
) -> FacilityRecord:
    retrieved = "2026-10-09T09:51:00Z"
    return make_record(
        external_ids={"epoch_name": [name]},
        parties={"tenant": [{"name": n, "source_ids": ["s1"]} for n in tenant]},
        location=location,
        sources=[
            {
                "id": "s1",
                "url": "https://epoch.ai/data/ai-data-centers",
                "publisher": "Epoch AI",
                "source_type": "open_dataset",
                "license": "CC-BY-4.0",
                "retrieved_at": retrieved,
                "supports": ["/canonical_name", "/location", "/capacity", "/parties"],
            },
            *(
                {
                    "id": f"s{i}",
                    "url": url,
                    "publisher": "example.org",
                    "source_type": "news",
                    "retrieved_at": retrieved,
                    "supports": [],
                }
                for i, url in enumerate(links, start=2)
            ),
        ],
    )


def test_an_epoch_site_an_override_places_in_the_rows_township_holds_the_row(
    make_context: MakeContext,
    make_record: Callable[..., FacilityRecord],
    counties: CountyIndex,
) -> None:
    # Epoch's AWS Berwick is placed by its override in Salem Township, at Luzerne County's
    # point (county precision): no distance rule reaches AI GridWatch's AWS Salem Township row.
    lat, lon = counties.centroid("42079")
    berwick = epoch_record(
        make_record,
        "AWS Berwick",
        {
            "lat": lat,
            "lon": lon,
            "precision": "county",
            "municipality": "Salem Township",
            "county_fips": "42079",
            "county_name": "Luzerne",
            "state_abbr": "PA",
            "geocode_method": "county_centroid",
        },
        tenant=("Amazon",),
    )
    result = run(make_context, records={berwick.id: berwick})
    assert "aws-salem-township-pa" not in records(result)
    (item,) = items(result, "aws-salem-township-pa", "possible_duplicate")
    assert (item.record_id, item.data["matched_by"], item.data["epoch_records"]) == (
        None,
        "place",
        [berwick.id],
    )
    # QTS Salem, in the same township, shares no organization with it.
    assert "qts-salem-salem-township-pa" not in {i.external_id for i in result.review}


def test_a_copy_tied_to_its_numbered_sibling_is_held_without_naming_it(
    make_context: MakeContext,
    make_record: Callable[..., FacilityRecord],
) -> None:
    # Epoch's QTS Richmond 2 and 3 have no record (they wait for an override); the rows that copy
    # them cite QTS Richmond 1's pages, so the rules tied them to Richmond 1.
    richmond_1 = epoch_record(
        make_record,
        "QTS Richmond 1",
        {
            "lat": 37.52,
            "lon": -77.33,
            "precision": "locality",
            "county_fips": "51087",
            "county_name": "Henrico",
            "state_abbr": "VA",
            "geocode_method": "gazetteer",
        },
        links=(
            "https://qtsdatacenters.com/data-centers/richmond-1/",
            "https://q.com/resources/new-builds-two-multi-story-data-centers-for-richmond-va/",
        ),
    )
    result = run(make_context, records={richmond_1.id: richmond_1})
    for pid in ("qts-richmond-2", "qts-richmond-3"):
        assert pid not in records(result)
        (item,) = items(result, pid, "possible_duplicate")
        assert (item.record_id, item.data["matched_by"]) == (None, "epoch_copy")
        assert "epoch_records" not in item.data and richmond_1.id not in item.reason
        assert not items(result, pid, "conflict")  # no stage compared with Richmond 1's
