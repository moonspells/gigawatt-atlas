"""Epoch AI timelines, parties and shared campuses, on real rows (tests/fixtures/epoch/cases.zip).

The notes quoted here are Epoch AI's ('AI data centers', epoch.ai, CC BY 4.0), as downloaded on
2026-10-08; see tests/fixtures/epoch/README.md.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from atlas.crosswalk import from_epoch_row
from atlas.geo.counties import CountyIndex
from atlas.schema.record import FacilityRecord
from atlas.schema.rollup import is_expanding
from atlas.sources.base import ImportContext, ImportResult, apply_import
from atlas.sources.epoch import (
    IMPORTER,
    Site,
    TimelineRow,
    address_key,
    status_events,
    timeline_coverage,
)
from atlas.store import RecordStore
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASES_ZIP = FIXTURES / "epoch" / "cases.zip"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides" / "epoch.json"
MakeContext = Callable[..., ImportContext]

# Coreweave Helios's first row, verbatim: the link URL holds "Announces".
HELIOS_2025_05_31 = (
    "The former [crypto mining](https://investor.galaxy.com/news-events/press-releases/detail/"
    "1002/galaxy-announces-commitment-with-coreweave-to-host-ai-and-hpc-infrastructure-at-"
    "helios-data-center-campus) Helios site begins expanding, with land cleared for additional "
    "buildings."
)
TODAY = date(2026, 10, 12)


def row(
    day: str, text: str, operational: float | None = 0, it_mw: float | None = None
) -> TimelineRow:
    return TimelineRow(date.fromisoformat(day), text, operational, it_mw, None, None)


def site(name: str, *, sources: str = "") -> Site:
    return Site(name, "", "", "", "", sources, None)


# The sites whose address places only through the Census, which these offline runs skip: a
# postal city gives no point without a stated county (07 §6.5). Each gets a test location at its
# county (made-up citation text) beside any committed entry it has (Google Fort Wayne's cited
# facility_status); a shared campus needs one for its first site. OpenAI Stargate Abilene, Google
# New Albany and QTS Richmond 1 have committed locations, which place them here.
TEST_COUNTIES = {
    "Coreweave Helios": ("TX", "Dickens"),
    "Core42 Lake Mariner": ("NY", "Niagara"),
    "CoreWeave Dalton 1 & 2": ("GA", "Whitfield"),
    "xAI QTS Atlanta": ("GA", "Fulton"),
    "CoreWeave Lancaster Greenfield site": ("PA", "Lancaster"),
    "Google Fort Wayne": ("IN", "Allen"),
}


def write_overrides(
    tmp_path: Path,
    counties: CountyIndex,
    test_counties: dict[str, tuple[str, str]],
    *,
    drop: tuple[tuple[str, str], ...] = (),
) -> Path:
    """The committed overrides, without the (site, key) parts in drop, plus a test county location
    for each site in test_counties."""
    entries = json.loads(REPO_OVERRIDES.read_text(encoding="utf-8"))
    for name, key in drop:
        del entries[name][key]
    for name, (state, county_name) in test_counties.items():
        county = counties.by_name(state, county_name)
        assert county is not None and "state_abbr" not in entries.get(name, {})
        entries.setdefault(name, {}).update(
            {
                "county_fips": county.fips,
                "note": "Test entry.",
                "precision": "county",
                "quote": f"The site is in {county.name} County.",
                "retrieved_at": "2026-10-08T05:00:00Z",
                "source_url": "https://example.org/site",
                "state_abbr": state,
            }
        )
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


@pytest.fixture
def overrides(tmp_path: Path, counties: CountyIndex) -> Path:
    """The committed overrides plus a county location for each site in TEST_COUNTIES."""
    return write_overrides(tmp_path, counties, TEST_COUNTIES)


@pytest.fixture
def cases(make_test_context: MakeContext, overrides: Path) -> ImportResult:
    ctx = make_test_context(input_path=CASES_ZIP)
    return IMPORTER.run(ctx, argparse.Namespace(overrides=overrides, no_geocode=True))


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    """Each candidate under every Epoch name it carries."""
    return {name: c.record for c in result.candidates for name in c.match_values}


def dates(rec: FacilityRecord) -> dict[str, str]:
    return {k: v.value for k, v in rec.dates.items()}


def test_every_case_record_is_valid(
    cases: ImportResult, counties: CountyIndex, today: date
) -> None:
    assert [i.kind for i in cases.review if i.kind == "invalid"] == []
    assert len(cases.candidates) == 11  # 13 sites, two pairs of which share a campus
    for c in cases.candidates:
        assert validate_record(c.record, counties=counties, today=today) == []


# ---------------------------------------------------------------------------- the status text


def test_the_status_text_is_read_without_its_link_urls(cases: ImportResult) -> None:
    # The URL's "Announces" made this row of physical work an announcement (07 §4.7).
    (event,) = status_events([row("2025-05-31", HELIOS_2025_05_31)], TODAY)
    assert event["status"] == "under_construction"
    assert event["note"] is not None and "https://" not in event["note"]
    # A URL alone never decides the status, whatever the text's own words are.
    linked = "Satellite [update](https://example.org/galaxy-announces-a-planned-campus) posted."
    assert from_epoch_row(linked, 0).status == "announced"  # what the URL would say
    (event,) = status_events([row("2025-05-31", linked)], TODAY)
    assert (event["status"], event["note"]) == ("under_construction", "Satellite update posted.")
    helios = records(cases)["Coreweave Helios"]
    assert [e.status for e in helios.status_history] == ["under_construction", "operating"]
    assert "announced" not in helios.dates


# ---------------------------------------------------------------------------- parties


def test_epochs_hardware_owner_is_a_tenant_never_the_facility_owner(cases: ImportResult) -> None:
    # Epoch: Owner is "Who owns the AI hardware in the data center. This is not necessarily the
    # owner or operator of the facility." CoreWeave leases Lancaster ("CoreWeave will be the
    # tenant of the site"), X Corp's equipment sits in QTS's building, and so on.
    for c in cases.candidates:
        assert c.record.parties.owner == [] and c.record.parties.operator == [], c.match_values
    tenants = {n: [o.name for o in r.parties.tenant] for n, r in records(cases).items()}
    assert tenants["CoreWeave Lancaster Greenfield site"] == ["CoreWeave"]
    assert tenants["xAI QTS Atlanta"] == ["SpaceXAI"]  # Owner and Users, once
    assert tenants["Coreweave Helios"] == ["CoreWeave"]  # OpenAI is #speculative
    assert tenants["OpenAI Stargate Abilene"] == ["Oracle", "OpenAI", "Microsoft"]
    assert tenants["Core42 Lake Mariner"] == ["Core42", "G42", "AI XPV Platform", "Anthropic"]


# ---------------------------------------------------------------------------- start dates


@pytest.mark.parametrize(
    ("text", "event"),
    [
        ("Land clearing begins for Building 1.", "construction_start"),
        (
            "Construction start - The Texas Department of Licensing and Regulation reports",
            "construction_start",
        ),
        ("Building 1 foundation started", "construction_start"),
        ("First signs of development/land clearing", "construction_start"),
        (
            "Core Scientific, CoreWeave, and Port Muskogee publicly announce groundbreaking for "
            "the 100 MW Muskogee HPC data center.",
            "construction_start",
        ),
        ("Land is cleared.", "first_reported"),
        ("Building 1 grading underway.", "first_reported"),
        (
            "X Corp's $700 million investment in AI training hardware for the Atlanta data center "
            "is approved by the Development Authority of Fulton County (bond resolution fact "
            "sheet).",
            "first_reported",
        ),
    ],
)
def test_a_first_row_dates_the_start_only_when_it_says_so(text: str, event: str) -> None:
    (got,) = status_events([row("2024-01-23", text)], TODAY)
    assert (got["status"], got["event"]) == ("under_construction", event)


def test_a_later_row_of_work_under_way_is_not_a_start(cases: ImportResult) -> None:
    # CoreWeave Lancaster: announced 2025-07-15, then "Cooling install continues on the roof".
    rec = records(cases)["CoreWeave Lancaster Greenfield site"]
    got = [(e.as_of.value, e.status, e.event, e.planned) for e in rec.status_history]
    assert got == [
        ("2025-07-15", "announced", "announced", False),
        ("2026-07-13", "under_construction", "other", False),
        ("2027-06", "operating", "energized", True),  # an estimate on the last day: the month
    ]
    assert rec.status == "under_construction"
    assert dates(rec) == {"announced": "2025-07-15", "first_reported": "2025-07-15"}


# ---------------------------------------------------------------------------- partial timelines


def test_a_first_row_that_already_counts_buildings_is_an_observation(cases: ImportResult) -> None:
    # Storey County's first row: "Building 1, which was first operational in 2021, ...".
    (event,) = status_events([row("2023-08-30", "Building 1 is operational.", 1)], TODAY)
    assert (event["status"], event["event"]) == ("operating", "other")
    for name in ("Google Storey County", "Google New Albany"):
        rec = records(cases)[name]
        assert rec.status == "operating" and rec.record_type == "campus"
        assert dates(rec) == {}, name  # neither operating_since nor first_reported
        # Epoch began tracking a running site, so its buildings are a phase of it (New Albany's
        # notes say nothing else about the campus).
        assert rec.capacity.it_mw is None and [p.capacity.it_mw for p in rec.phases] != [], name


@pytest.mark.parametrize(
    ("rows", "evidence"),
    [
        (  # QTS Richmond 1
            [
                row(
                    "2023-10-01",
                    "Land cleared for site expansion, some materials down at substation site.",
                )
            ],
            "site expansion",
        ),
        ([row("2020-03-04", "Land clearing begins for Building 1 of expansion.")], "of expansion"),
        ([row("2024-07-24", "Land clearing starts for new building.")], "new building"),
        ([row("2025-05-31", HELIOS_2025_05_31)], "former crypto mining"),
        (
            [
                row(
                    "2024-11-01",
                    "Construction start - The Texas Department of Licensing and Regulation "
                    "reports Core Scientific is converting existing Bitcoin mining buildings into "
                    "AI data centers",
                )
            ],
            "existing Bitcoin",
        ),
        (
            [
                row(
                    "2024-06-03",
                    "According to a few sources, Core Scientific announced that they're "
                    "rebuilding their existing Dalton 1 (and we're assuming 2) datacenter to run "
                    "AI using CoreWeave's GPUs",
                )
            ],
            "existing Dalton 1",
        ),
        (
            [
                row(
                    "2023-09-18",
                    "Conversion starts at CTP-01 (Building 1). The data center was formerly "
                    "owned by Capital One, now leased to CoreWeave.",
                )
            ],
            "formerly owned",
        ),
        (
            [
                row("2024-01-23", "Bond resolution approved."),
                row(
                    "2025-02-20",
                    "xAI occupies part of this multi-tenant building, so its cooling is not "
                    "attributed to xAI alone.",
                    1,
                ),
            ],
            "multi-tenant",
        ),
        (
            [
                row(
                    "2022-10-30",
                    "Construction beginning, as can be seen on satellite. Major renovations on "
                    "western side of the non AI building (PX1), including adding generators and "
                    "cooling towers.",
                )
            ],
            "non AI building",
        ),
        (
            [
                row("2024-04-01", "First signs of development/land clearing"),
                row(
                    "2024-09-13",
                    "Roof complete on Building 1 (the 'H'), although we suspect this building is "
                    "not for AI compute due to the construction speed, irregular design, and lack "
                    "of external cooling.",
                ),
            ],
            "not for AI compute",
        ),
        ([row("2023-08-30", "Building 1 is operational.", 1)], "Building 1 is operational"),
    ],
)
def test_notes_that_place_the_tracked_buildings_in_an_older_facility(
    rows: list[TimelineRow], evidence: str
) -> None:
    coverage = timeline_coverage([(site("Test site"), rows)], TODAY)
    assert coverage.partial and not coverage.held
    assert coverage.evidence is not None and evidence in coverage.evidence


@pytest.mark.parametrize(
    "rows",
    [
        # Colossus 2 is a new campus; the "existing building" is the one being built out, and the
        # later row's "expansion" is the site's own growth.
        [
            row("2025-02-28", "Land clearing begins next to the existing building."),
            row(
                "2026-06-15",
                'corresponding to the "next phase of expansion" found in the following company '
                "S-1 filing",
                2,
            ),
        ],
        [  # Meta Bowling Green
            row("2024-11-01", "Land clearing begins."),
            row(
                "2026-02-14",
                "Optimus substation, which is the substation on site, and the expansion of "
                "Dowling Substation, an existing substation to the northeast of the data center, "
                "appear complete.",
            ),
        ],
        [
            row(
                "2025-07-15",
                "Coreweave announces $6 billion investment into Lancaster data centers, at two former printing press sites.",
            )
        ],
        [
            row(
                "2024-04-24",
                "Construction starts with a road added on site, retrofitting a former Electrolux building.",
            )
        ],
    ],
)
def test_notes_about_a_new_site_do_not_make_it_partial(rows: list[TimelineRow]) -> None:
    assert timeline_coverage([(site("Test site"), rows)], TODAY) == timeline_coverage(
        [(site("Test site"), [row("2025-01-01", "Land clearing begins.")])], TODAY
    )
    assert not timeline_coverage([(site("Test site"), rows)], TODAY).observed


def test_a_partial_timeline_is_a_phase_and_dates_nothing(cases: ImportResult) -> None:
    # QTS bought the Richmond site in 2010; Epoch's timeline starts with the 2023 expansion and the
    # retrofit of Buildings 1 and 2, so it gives neither the campus's start nor its first operation.
    rec = records(cases)["QTS Richmond 1"]
    assert rec.status == "operating" and rec.record_type == "campus"
    assert dates(rec) == {}
    assert {e.event for e in rec.status_history} == {"other"}
    (phase,) = rec.phases
    assert phase.phase_id == "qts-richmond-1"
    assert phase.name == "QTS Richmond 1 (buildings tracked by Epoch AI)"
    assert (phase.capacity.it_mw, phase.capacity.facility_mw) == (238.0, 333.0)
    assert phase.source_ids == ["s1"]
    assert all(e.phase_id == "qts-richmond-1" for e in rec.status_history)
    # The tracked buildings' capacity, water use and cost are not the facility's.
    assert rec.capacity.it_mw is None and rec.capacity.facility_mw is None
    assert rec.money.investment_usd is None and rec.cooling.water_use_mgd is None
    assert not [p for p in rec.field_meta if p.startswith(("/capacity", "/money", "/cooling"))]
    for name in ("Coreweave Helios", "CoreWeave Dalton 1 & 2", "xAI QTS Atlanta"):
        other = records(cases)[name]
        assert dates(other) == {} and other.capacity.it_mw is None and other.phases, name
    # Helios, Richmond 1, Storey County, New Albany, Fort Wayne, Dalton, xAI and Lake Mariner.
    assert cases.metrics["timelines_partial"] == 8


def test_a_partial_timeline_behind_the_facility_is_held_for_review(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # Epoch's count leaves Google Fort Wayne's Building 1 out as probably not for AI compute, and
    # its tracked buildings are under construction; Google said on 2025-12-11 that the data center
    # is operational. Without a cited facility status the tracked buildings' status is not the
    # facility's, so there is no record: a conflict item says why.
    path = write_overrides(
        tmp_path, counties, TEST_COUNTIES, drop=(("Google Fort Wayne", "facility_status"),)
    )
    ctx = make_test_context(input_path=CASES_ZIP)
    result = IMPORTER.run(ctx, argparse.Namespace(overrides=path, no_geocode=True))
    assert "Google Fort Wayne" not in records(result)
    (item,) = [i for i in result.review if i.external_id == "Google Fort Wayne"]
    assert item.kind == "conflict" and "its status is unknown: no record" in item.reason
    assert "not for AI compute" in str(item.data["evidence"])
    assert item.data["status"] == "under_construction"
    assert result.metrics["timelines_status_unknown"] == 1
    # Partial timelines whose tracked buildings operate need no item.
    assert not [i for i in result.review if i.external_id == "QTS Richmond 1"]


def test_a_cited_facility_status_is_the_facility_s(cases: ImportResult) -> None:
    # The committed entry cites WPTA's report of Google's announcement; it goes on a phase of the
    # buildings Epoch does not track, as an observation that dates nothing.
    rec = records(cases)["Google Fort Wayne"]
    assert rec.status == "operating" and rec.record_type == "campus" and is_expanding(rec)
    assert dates(rec) == {} and rec.capacity.it_mw is None
    assert [p.phase_id for p in rec.phases] == ["google-fort-wayne", "google-fort-wayne-untracked"]
    assert rec.phases[1].name == "Google Fort Wayne (buildings Epoch AI does not track)"
    (cited,) = [e for e in rec.status_history if e.phase_id == "google-fort-wayne-untracked"]
    assert (cited.as_of.value, cited.status, cited.event) == ("2025-12-11", "operating", "other")
    source = next(s for s in rec.sources if s.id == cited.source_ids[0])
    assert source.quote is not None and "Fort Wayne data center is operational" in source.quote
    assert source.supports == ["/status_history"] and rec.phases[1].source_ids == [source.id]
    tracked = {e.status for e in rec.status_history if e.phase_id == "google-fort-wayne"}
    assert tracked == {"under_construction", "operating"}  # the planned Building 2
    assert not [i for i in cases.review if i.external_id == "Google Fort Wayne"]


def test_a_cited_expansion_is_held_for_review(cases: ImportResult) -> None:
    # Meta Huntsville opened in 2021; Epoch's timeline (from land clearing in December 2021) is
    # the expansion its first Selected Source announces, which Epoch's fields cannot place.
    expansion = "Announcement of expansion completing in 2026"
    coverage = timeline_coverage(
        [
            (
                site("Test", sources=f"- [{expansion}](https://example.org/x)"),
                [row("2021-12-13", "Building 1 land clearing begins.")],
            )
        ],
        TODAY,
    )
    assert coverage.held and not coverage.partial and coverage.evidence == expansion
    rec = records(cases)["Meta Huntsville"]
    assert dates(rec) == {} and rec.capacity.it_mw is None
    (phase,) = rec.phases
    assert phase.capacity.it_mw == 146.0
    (item,) = [i for i in cases.review if i.external_id == "Meta Huntsville"]
    assert item.kind == "conflict" and item.data["evidence"] == expansion
    assert "restore them if the timeline covers the whole facility" in item.reason
    assert cases.metrics["timelines_held"] == 1


# ---------------------------------------------------------------------------- shared campuses


def test_sites_at_one_street_address_are_one_campus(cases: ImportResult) -> None:
    # TeraWulf's "Lake Mariner Data Campus" has two tenants; Epoch lists each as a site, at the
    # same address. 07 §2.1: one campus, a phase per site.
    by_names = {c.match_values: c.record for c in cases.candidates}
    lake = by_names[("Core42 Lake Mariner", "Anthropic Lake Mariner")]
    assert lake.external_ids == {"epoch_name": ["Core42 Lake Mariner", "Anthropic Lake Mariner"]}
    assert lake.canonical_name == "Core42 Lake Mariner (Niagara County, NY)"
    assert [p.phase_id for p in lake.phases] == ["core42-lake-mariner", "anthropic-lake-mariner"]
    assert [(a.name, a.kind) for a in lake.aliases] == [("Anthropic Lake Mariner", "phase_name")]
    # Lake Mariner is a Bitcoin mining campus ("245 MW of Bitcoin-mining capacity"): partial.
    assert dates(lake) == {} and lake.capacity.it_mw is None
    assert [p.capacity.it_mw for p in lake.phases] == [58.0, 42.0]

    abilene = by_names[("OpenAI Stargate Abilene", "Crusoe Abilene Expansion")]
    assert abilene.status == "operating" and is_expanding(abilene)
    # first_reported: the committed first_report (Trade & Industry Development, 2021-12-22, on
    # Lancium's campus), earlier than Epoch's first row.
    assert dates(abilene) == {
        "first_reported": "2021-12-22",
        "construction_start": "2024-05-31",
        "operating_since": "2025-09-26",
    }
    by_phase = {
        e.phase_id: (e.as_of.value, e.status, e.event)
        for e in abilene.status_history
        if not e.planned and e.status == "under_construction"
    }
    assert by_phase == {
        "openai-stargate-abilene": ("2024-05-31", "under_construction", "construction_start"),
        "crusoe-abilene-expansion": ("2025-11-02", "under_construction", "construction_start"),
    }
    assert [e.seq for e in abilene.status_history] == list(
        range(1, len(abilene.status_history) + 1)
    )
    assert abilene.capacity.it_mw == 421.0  # the expansion has no current capacity
    assert [p.capacity.it_mw for p in abilene.phases] == [421.0, None]
    assert cases.metrics["sites_in_shared_campuses"] == 4


def test_address_keys() -> None:
    assert address_key("7725 Lake Rd, Barker, NY 14012") == address_key(
        "7725 lake rd barker ny 14012"
    )
    assert address_key("1772-2396 145th St, Rosemount, MN 55068, USA") == (
        "1772 2396 145th st rosemount mn 55068"
    )
    assert address_key("Holly Ridge, LA 71269") is None
    assert address_key("Co Rd 42, Montgomery, AL 36105, USA") is None
    assert address_key("") is None


def test_a_shared_campus_is_written_once_and_found_again(
    make_test_context: MakeContext, overrides: Path, tmp_repo: Path
) -> None:
    store = RecordStore(tmp_repo / "data" / "records")
    review_dir, receipts = tmp_repo / "review" / "queue", tmp_repo / "data" / "imports"
    args = argparse.Namespace(overrides=overrides, no_geocode=True)
    ctx = make_test_context(input_path=CASES_ZIP)
    receipt = apply_import(
        IMPORTER,
        IMPORTER.run(ctx, args),
        ctx,
        store=store,
        review_dir=review_dir,
        receipts_dir=receipts,
    )
    assert receipt.counts["new"] == 11 and receipt.counts["conflicts"] == 0
    again = make_test_context(input_path=CASES_ZIP, records=store.load())
    receipt2 = apply_import(
        IMPORTER,
        IMPORTER.run(again, args),
        again,
        store=store,
        review_dir=review_dir,
        receipts_dir=receipts,
    )
    assert receipt2.counts["unchanged"] == 11 and receipt2.counts["conflicts"] == 0
