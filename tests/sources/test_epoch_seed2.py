"""Epoch AI rules from the second seed check (2026-10-09), on real rows
(tests/fixtures/epoch/cases-2026-10-09.zip): dates on the 1st of a month, a cited first report,
new buildings on an operating campus, a bitcoin miner's site, and the committed entries that cite
them.

The notes quoted here are Epoch AI's ('AI data centers', epoch.ai, CC BY 4.0), as downloaded on
2026-10-09; see tests/fixtures/epoch/README.md.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.geocode import Gazetteer
from atlas.net import FetchError
from atlas.schema.record import FacilityRecord
from atlas.sources.base import ImportContext, ImportResult
from atlas.sources.epoch import (
    IMPORTER,
    FirstReport,
    LocationOverride,
    Site,
    TimelineRow,
    _first_report_event,
    load_overrides,
    place_override,
    row_date,
    timeline_coverage,
)
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASES_ZIP = FIXTURES / "epoch" / "cases-2026-10-09.zip"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides" / "epoch.json"
MakeContext = Callable[..., ImportContext]
TODAY = date(2026, 10, 9)

# Sites whose address places only through the Census (skipped offline): a test location at their
# county, beside any committed entry (Meta Sarpy's cited timeline).
TEST_COUNTIES = {
    "Google Council Bluffs (East)": ("IA", "Pottawattamie"),
    "CoreWeave Marble NC": ("NC", "Cherokee"),
    "Meta Sarpy": ("NE", "Sarpy"),
}


def row(day: str, text: str, operational: float | None = 0) -> TimelineRow:
    return TimelineRow(date.fromisoformat(day), text, operational, None, None, None)


def site(name: str = "Test site", *, owner: str = "", sources: str = "") -> Site:
    return Site(name, "", owner, "", "", sources, None)


def write_overrides(
    tmp_path: Path, counties: CountyIndex, *, edit: Callable[[dict[str, Any]], None] | None = None
) -> Path:
    entries = json.loads(REPO_OVERRIDES.read_text(encoding="utf-8"))
    for name, (state, county_name) in TEST_COUNTIES.items():
        county = counties.by_name(state, county_name)
        assert county is not None and "state_abbr" not in entries.get(name, {})
        entries.setdefault(name, {}).update(
            {
                "county_fips": county.fips,
                "note": "Test entry.",
                "precision": "county",
                "quote": f"The site is in {county.name} County.",
                "retrieved_at": "2026-10-09T05:00:00Z",
                "source_url": "https://example.org/site",
                "state_abbr": state,
            }
        )
    if edit is not None:
        edit(entries)
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def run(make_test_context: MakeContext, overrides: Path) -> ImportResult:
    ctx = make_test_context(input_path=CASES_ZIP, today=TODAY)
    return IMPORTER.run(ctx, argparse.Namespace(overrides=overrides, no_geocode=True))


@pytest.fixture
def seed2(make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex) -> ImportResult:
    return run(make_test_context, write_overrides(tmp_path, counties))


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    return {name: c.record for c in result.candidates for name in c.match_values}


def dates(rec: FacilityRecord) -> dict[str, str]:
    return {k: v.value for k, v in rec.dates.items()}


def items(result: ImportResult, name: str) -> list[tuple[str, str]]:
    return [(i.kind, i.reason) for i in result.review if i.external_id == name]


def test_every_case_record_is_valid(seed2: ImportResult, counties: CountyIndex) -> None:
    assert [i for i in seed2.review if i.kind in ("invalid", "geocode_failed")] == []
    assert len(seed2.candidates) == 10
    for c in seed2.candidates:
        assert validate_record(c.record, counties=counties, today=TODAY) == [], c.match_values


# ---------------------------------------------------------------------------- dates


def test_a_date_on_the_first_of_a_month_is_the_month(seed2: ImportResult) -> None:
    # Google Kansas City East: "Building 1 operational. Estimated based on present construction
    # progress and typical timelines." on 2026-08-01; the first row is 2024-01-01.
    # (first_reported is the committed first report: KSHB, 2019-07-22, a day.)
    rec = records(seed2)["Google Kansas City East"]
    assert dates(rec) == {
        "first_reported": "2019-07-22",
        "construction_start": "2024-01",
        "operating_since": "2026-08",
    }
    assert {k: d.precision for k, d in rec.dates.items() if k != "first_reported"} == {
        "construction_start": "month",
        "operating_since": "month",
    }
    operating = next(e for e in rec.status_history if e.status == "operating")
    assert (operating.as_of.value, operating.as_of.precision) == ("2026-08", "month")


@pytest.mark.parametrize(
    ("day", "text", "value"),
    [
        (
            "2026-08-01",
            "Building 1 operational. Estimated based on present construction.",
            "2026-08",
        ),
        ("2024-01-01", "Land clearing has begun for the site", "2024-01"),
        ("2026-06-30", "Building 3 is operational (estimate).", "2026-06"),
        ("2025-06-15", "Building 2 is operational. Assuming it takes 3 months after", "2025-06"),
        ("2024-02-29", "We think Building 2 is roofed.", "2024-02"),
        # A day on the 15th or the last of a month without an estimate, or an estimate on any
        # other day, keeps the day: Epoch's imagery has dates.
        ("2024-05-15", "Ground is being cleared.", "2024-05-15"),
        (
            "2024-05-31",
            "Land clearing begins for northern two buildings (Building 1-2)",
            "2024-05-31",
        ),
        ("2024-11-19", "Building 1 is operational. So our best guess is", "2024-11-19"),
    ],
)
def test_epoch_estimates_are_read_as_the_month(day: str, text: str, value: str) -> None:
    got = row_date(row(day, text))
    assert got == {"value": value, "precision": "month" if len(value) == 7 else "day"}


def test_a_cited_earlier_report_dates_first_reported(seed2: ImportResult) -> None:
    # MPR News (2023-09-02) reported the utility filing for Meta's Rosemount data center six
    # months before Meta's "Hello, Rosemount!" and nine before Epoch's first row ("Land is
    # cleared", 2024-05-30); the entry cites it, a page that is not among Epoch's sources.
    rose = records(seed2)["Meta Rosemount"]
    assert dates(rose) == {"first_reported": "2023-09-02", "operating_since": "2026-06-19"}
    first = rose.status_history[0]
    assert (first.event, first.status, first.phase_id) == ("first_reported", "proposed", None)
    mpr = next(s for s in rose.sources if s.id == first.source_ids[0])
    assert (mpr.publisher, mpr.supports) == ("MPR News", ["/status_history"])
    assert mpr.published_at is not None
    assert mpr.published_at.isoformat() == "2023-09-02T16:30:00-05:00"
    # Meta's page stays the location source only.
    assert rose.sources[1].supports == ["/location"]
    # Epoch's first row is now an observation; it dates nothing.
    observed = next(e for e in rose.status_history if e.source_ids == ["s1"])
    assert (observed.as_of.value, observed.event) == ("2024-05-30", "other")
    # Google The Dalles: Columbia Community Connection (2021-10-19) reported Google's
    # two-data-center proposal a year before the City's notice of decision (2022-10-31), one of
    # Epoch's Selected Sources and the entry's first report until then.
    dalles = records(seed2)["Google The Dalles"]
    assert dates(dalles) == {
        "first_reported": "2021-10-19",
        "construction_start": "2023-05-21",
        "operating_since": "2025-03-07",
    }
    report = dalles.status_history[0]
    assert (report.event, report.status) == ("first_reported", "proposed")
    ccc = next(s for s in dalles.sources if s.id == report.source_ids[0])
    assert ccc.publisher == "Columbia Community Connection" and ccc.supports == ["/status_history"]
    assert ccc.quote is not None and "two data plants" in ccc.quote
    notice = next(s for s in dalles.sources if "SPR" in str(s.url))
    assert notice.supports == []
    assert seed2.metrics["first_reports_cited"] == 3  # with Google Kansas City East's


def test_a_report_that_is_not_earlier_dates_nothing() -> None:
    def report(day: str, status: str = "announced") -> FirstReport:
        return FirstReport.model_validate(
            {
                "source_url": "https://example.org/r",
                "quote": "x",
                "retrieved_at": "2026-10-09T00:00:00Z",
                "note": "x",
                "published_at": day,
                "status": status,
            }
        )

    events = [
        {
            "status": "announced",
            "as_of": {"value": "2024-05", "precision": "month"},
            "planned": False,
        },
        {
            "status": "operating",
            "as_of": {"value": "2023-01", "precision": "month"},
            "planned": True,
        },
    ]
    assert _first_report_event(report("2024-04-30"), events) is not None
    assert _first_report_event(report("2024-05-01"), events) is None  # the month has begun
    assert _first_report_event(report("2024-04-30", "permitted"), events) is None  # ahead of it


# ---------------------------------------------------------------------------- coverage


def test_a_site_named_as_part_of_a_campus_is_held(seed2: ImportResult) -> None:
    # "Land clearing begins for east buildings": the buildings Google added beside its older
    # Bunge Avenue campus. Epoch's fields do not say whose they are, so the timeline is held.
    rec = records(seed2)["Google Council Bluffs (East)"]
    assert rec.status == "operating" and dates(rec) == {}
    assert rec.capacity.it_mw is None and rec.money.investment_usd is None
    assert [(p.capacity.it_mw, p.capacity.facility_mw) for p in rec.phases] == [(237.0, 284.0)]
    ((kind, reason),) = items(seed2, "Google Council Bluffs (East)")
    assert kind == "conflict" and reason.startswith(
        "the name or the first row names part of a site"
    )


@pytest.mark.parametrize(
    ("name", "first", "held"),
    [
        ("Google Pryor (North)", "Land clearing begins.", True),
        ("Test site", "Land clearing begins for east buildings", True),
        ("Test site", "Land clearing begins for Buildings 5-9 (eastern campus).", True),
        ("Test site", "Land clearing begins for northern two buildings (Building 1-2).", False),
        ("Test site", "Land clearing begins for southern part (Building 1-2)", False),
        ("Test site", "Land clearing begins for Building 1.", False),
    ],
)
def test_part_of_a_site(name: str, first: str, held: bool) -> None:
    coverage = timeline_coverage([(site(name), [row("2022-09-06", first)])], TODAY)
    assert coverage.held is held and not coverage.partial


@pytest.mark.parametrize(
    ("rows", "evidence"),
    [
        (  # CoreWeave Marble NC: Core Scientific's bitcoin site since 2018
            [
                row(
                    "2024-06-25",
                    "Core Scientific announced that CoreWeave would receive about 70 MW of HPC "
                    "infrastructure from 100 MW of owned infrastructure. Site modifications were "
                    "expected to begin in H2 2024.",
                )
            ],
            "of owned infrastructure",
        ),
        (  # AWS New Albany
            [
                row("2022-07-01", "Land clearing begins for Buildings 5-9 (eastern campus)."),
                row(
                    "2024-12-01",
                    "Buildings 5-9 (eastern campus) operational. Note that we're only including "
                    "roughly 44% of all the New Albany campuses IT Power",
                    5,
                ),
            ],
            "only including",
        ),
    ],
)
def test_notes_of_a_conversion_or_a_part(rows: list[TimelineRow], evidence: str) -> None:
    coverage = timeline_coverage([(site(), rows)], TODAY)
    assert coverage.partial and coverage.evidence is not None and evidence in coverage.evidence


def test_a_bitcoin_site_conversion_dates_nothing(seed2: ImportResult) -> None:
    marble = records(seed2)["CoreWeave Marble NC"]
    assert marble.status == "operating" and dates(marble) == {}
    assert marble.capacity.it_mw is None and marble.capacity.facility_mw is None
    assert [(p.capacity.it_mw, p.capacity.facility_mw) for p in marble.phases] == [(65.0, 91.0)]
    assert {e.event for e in marble.status_history} == {"other"}
    # Cipher Mining's Barber Lake: a miner's site, which Epoch's fields do not decide.
    barber = records(seed2)["Anthropic Barber Lake"]
    assert dates(barber) == {} and barber.capacity.it_mw is None
    ((kind, reason),) = items(seed2, "Anthropic Barber Lake")
    assert kind == "conflict" and reason.startswith("the site is a bitcoin miner's (Cipher Mining)")
    assert (
        next(i for i in seed2.review if i.external_id == "Anthropic Barber Lake").data["evidence"]
        == "Owner: Cipher Mining #confident"
    )
    # A miner named only in a link's URL does not count.
    linked = "Land clearing begins ([report](https://example.org/core-scientific-news))."
    assert not timeline_coverage([(site(), [row("2025-01-02", linked)])], TODAY).observed
    # Microsoft SAT40: Epoch's own link title says it counts only the AI building of three.
    sat40 = records(seed2)["Microsoft SAT40"]
    assert dates(sat40) == {} and sat40.capacity.it_mw is None
    for name in ("CoreWeave Marble NC", "AWS New Albany"):
        assert items(seed2, name) == [], name
    # SAT40's only item is the location conflict its entry records (TDLR's SAT11-14: Medina).
    assert [kind for kind, _ in items(seed2, "Microsoft SAT40")] == ["conflict"]


def test_a_cited_timeline_entry_makes_a_new_building_a_phase(
    seed2: ImportResult, make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # Meta Sarpy: Meta's fact sheet (Epoch's Selected Source) says "2017 Broke ground on the Sarpy
    # Data Center"; Epoch's Building 1 starts in 2024. Google Arcola: Bisnow (2024-04-29) says
    # Google "operates a 400K SF data center in Arcola" before Epoch's Building 1 foundation.
    for name in ("Meta Sarpy", "Google Arcola"):
        rec = records(seed2)[name]
        assert rec.status == "operating" and dates(rec) == {}, name
        assert rec.capacity.it_mw is None and rec.money.investment_usd is None, name
        assert [p.capacity.it_mw for p in rec.phases] != [None], name
        assert items(seed2, name) == [], name
    sarpy = records(seed2)["Meta Sarpy"]
    sheet = next(s for s in sarpy.sources if "/phases" in s.supports)
    assert sheet.title == "Meta Sarpy info sheet (PDF)" and sheet.publisher == "Meta"
    assert sheet.quote == "2017 Broke ground on the Sarpy Data Center"
    arcola = records(seed2)["Google Arcola"]
    assert arcola.sources[1].supports == ["/location", "/phases"]  # Bisnow holds both
    # A reviewer's "whole" entry restores the facility's dates and capacity.

    def whole(entries: dict[str, Any]) -> None:
        entries["Google Council Bluffs (East)"]["timeline"] = {
            **entries["Meta Sarpy"]["timeline"],
            "coverage": "whole",
            "quote": "Test: the east buildings are the whole facility.",
        }

    result = run(make_test_context, write_overrides(tmp_path, counties, edit=whole))
    rec = records(result)["Google Council Bluffs (East)"]
    assert dates(rec) == {
        "first_reported": "2022-09-06",
        "construction_start": "2022-09-06",
        "operating_since": "2024-11-19",
    }
    assert rec.capacity.it_mw == 237.0 and rec.phases == []
    assert items(result, "Google Council Bluffs (East)") == []


# ---------------------------------------------------------------------------- locations


def test_a_postal_city_needs_a_place_check(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # Rosemount is also the city of Epoch's address (1772-2396 145th St, Rosemount, MN): an entry
    # placing the site there must say how the site was found inside Rosemount's place polygon.
    def unchecked(entries: dict[str, Any]) -> None:
        del entries["Meta Rosemount"]["place_check"]

    with pytest.raises(FetchError, match=r"'Meta Rosemount': 'Rosemount' is also the postal city"):
        run(make_test_context, write_overrides(tmp_path, counties, edit=unchecked))


def committed(name: str) -> LocationOverride:
    entry = load_overrides(REPO_OVERRIDES)[name].location
    assert entry is not None
    return entry


def test_google_pryor_is_in_mayes_county_not_its_postal_town(
    counties: CountyIndex, sample_gazetteer: Gazetteer
) -> None:
    # 4581 Webb St, Pryor: Google's Mayes County campus lies outside the Pryor Creek place polygon.
    ov = committed("Google Pryor (North)")
    placed = place_override(
        "Google Pryor (North)",
        ov,
        counties=counties,
        gazetteer=sample_gazetteer,
        postal_city="Pryor",
    )
    loc = placed.location
    assert (loc["precision"], loc["county_fips"], loc["city"], loc["municipality"]) == (
        "county",
        "40097",
        None,
        None,
    )
    assert placed.city_or_county == "Mayes County"
    assert "Mayes County" in ov.quote and str(ov.source_url).startswith(
        "https://datacenters.google/"
    )


def test_a_township_is_placed_at_its_own_point(
    counties: CountyIndex, sample_gazetteer: Gazetteer
) -> None:
    # AWS Berwick is in Salem Township, Luzerne County; the county's point is in Rice township,
    # 17 km away, so the township gives the point.
    ov = committed("AWS Berwick")
    placed = place_override("AWS Berwick", ov, counties=counties, gazetteer=sample_gazetteer)
    loc = placed.location
    assert (loc["precision"], loc["municipality"], loc["county_fips"], loc["geocode_method"]) == (
        "locality",
        "Salem Township",
        "42079",
        "gazetteer",
    )
    assert (loc["lat"], loc["lon"]) == (41.105979, -76.183579)  # Salem township, GEOID 4207967456
    assert counties.contains("42079", loc["lat"], loc["lon"])
    assert placed.city_or_county == "Salem Township"
    # A county entry never names a municipality or a city, which its point need not lie in.
    for field in ("municipality", "city"):
        bad = ov.model_copy(update={"precision": "county", "municipality": None, field: "Salem"})
        with pytest.raises(FetchError, match="a county entry names no city or municipality"):
            place_override("AWS Berwick", bad, counties=counties, gazetteer=sample_gazetteer)
    elsewhere = ov.model_copy(update={"county_fips": "42037"})  # Columbia County has no Salem
    with pytest.raises(FetchError, match="no Gazetteer county subdivision 'Salem Township'"):
        place_override("AWS Berwick", elsewhere, counties=counties, gazetteer=sample_gazetteer)
