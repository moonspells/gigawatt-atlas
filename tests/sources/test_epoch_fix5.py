"""Epoch AI rules from the second check of the round-4 seed (fix round 5, 2026-10-09), on real
rows (tests/fixtures/epoch/cases-fix5.zip) and the committed entries that cite them: first_reported
is the earliest dated report among the importer's inputs (Selected Sources, entries, Epoch's
notes, TDLR registrations), not Epoch's first observation; Epoch's modelled capacity yields to a
stated one; a note can date the start of operation earlier than its row; a location conflict
among the cited sources, or a cited source that states another status, is recorded for review;
notes keep whole URLs or none; and cited milestones.

The notes quoted here are Epoch AI's ('AI data centers', epoch.ai, CC BY 4.0), as downloaded on
2026-10-09; see tests/fixtures/epoch/README.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from atlas.geo.counties import CountyIndex
from atlas.schema.record import FacilityRecord
from atlas.sources.base import ImportContext, ImportResult
from atlas.sources.epoch import (
    FIRST_OBSERVATION,
    IMPORTER,
    Site,
    TimelineRow,
    event_note,
    noted_announcements,
    operation_period,
    operation_review,
    parse_period,
    selected_date,
    stated_capacity,
    status_events,
    strip_urls,
)
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASES_ZIP = FIXTURES / "epoch" / "cases-fix5.zip"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "overrides" / "epoch.json"
MakeContext = Callable[..., ImportContext]
TODAY = date(2026, 10, 9)

# Sites whose address places only through the Census (skipped offline): a test location at their
# county, beside their committed parts.
TEST_COUNTIES = {
    "Microsoft-Nebius New Jersey": ("NJ", "Cumberland"),
    "OpenAI Stargate Lordstown": ("OH", "Trumbull"),
    "CoreWeave Ellendale ND": ("ND", "Dickey"),
    "xAI QTS Atlanta": ("GA", "Fulton"),
    "Microsoft SAT14": ("TX", "Bexar"),
    "CoreWeave Chester VA": ("VA", "Chesterfield"),
    "CoreWeave Dalton 1 & 2": ("GA", "Whitfield"),
    "Google Bristow": ("VA", "Prince William"),
    "Vantage TX1": ("TX", "Bexar"),
    "CoreWeave Lancaster Greenfield site": ("PA", "Lancaster"),
}
TDLR = "https://www.tdlr.texas.gov/TABS/Search/Project/"


def row(day: str, text: str, operational: float | None = 0) -> TimelineRow:
    return TimelineRow(date.fromisoformat(day), text, operational, None, None, None)


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


def run(
    make_test_context: MakeContext, overrides: Path, zip_path: Path = CASES_ZIP
) -> ImportResult:
    ctx = make_test_context(input_path=zip_path, today=TODAY)
    return IMPORTER.run(ctx, argparse.Namespace(overrides=overrides, no_geocode=True))


@pytest.fixture
def fix5(make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex) -> ImportResult:
    return run(make_test_context, write_overrides(tmp_path, counties))


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    return {name: c.record for c in result.candidates for name in c.match_values}


def dates(rec: FacilityRecord) -> dict[str, str]:
    return {k: v.value for k, v in rec.dates.items()}


def items(result: ImportResult, name: str) -> list[tuple[str, str]]:
    return [(i.kind, i.reason) for i in result.review if i.external_id == name]


def first_event(rec: FacilityRecord) -> Any:
    return min(
        (e for e in rec.status_history if not e.planned and e.event != "other"),
        key=lambda e: (e.as_of.value, e.seq),
    )


def source(rec: FacilityRecord, sid: str) -> Any:
    return next(s for s in rec.sources if s.id == sid)


def test_every_case_record_is_valid(fix5: ImportResult, counties: CountyIndex) -> None:
    assert [i for i in fix5.review if i.kind in ("invalid", "geocode_failed")] == []
    assert len(fix5.candidates) == 16
    for c in fix5.candidates:
        assert validate_record(c.record, counties=counties, today=TODAY) == [], c.match_values


# ---------------------------------------------------------------------------- first reports


def test_a_cited_report_earlier_than_the_imagery_dates_first_reported(fix5: ImportResult) -> None:
    # Google Mesa: the City of Mesa's release of 2019-07-01 on the council's development agreement,
    # four years before Epoch's first row (land clearing, 2023-07), which stays the construction
    # start.
    mesa = records(fix5)["Google Mesa"]
    assert dates(mesa) == {
        "first_reported": "2019-07-01",
        "construction_start": "2023-07",
        "operating_since": "2025-07",
    }
    report = first_event(mesa)
    assert (report.event, report.status) == ("first_reported", "announced")
    cited = source(mesa, report.source_ids[0])
    assert (cited.publisher, cited.source_type) == ("City of Mesa", "government_record")
    assert next(e.event for e in mesa.status_history if e.source_ids == ["s1"]) == (
        "construction_start"
    )
    # Microsoft SAT14: Epoch's own Selected Source, TDLR's registration of SAT14 - Phase 1
    # (Registration Date 7/6/2021), seven months before its first row (2022-02-18).
    sat14 = records(fix5)["Microsoft SAT14"]
    assert dates(sat14)["first_reported"] == "2021-07-06"
    registration = source(sat14, first_event(sat14).source_ids[0])
    assert str(registration.url) == TDLR + "TABS2021019142"
    assert registration.title == "Phase 1 TDLR" and registration.supports == ["/status_history"]
    assert items(fix5, "Microsoft SAT14") == []


def test_an_entry_that_is_not_earlier_keeps_epochs_observation(fix5: ImportResult) -> None:
    # Vantage TX1: TDLR registered TX11 on 12/15/2023, the day of Epoch's first row; the entry
    # records that check, and the row stays first_reported, its note saying what it is.
    tx1 = records(fix5)["Vantage TX1"]
    assert dates(tx1)["first_reported"] == "2023-12-15"
    first = first_event(tx1)
    assert first.source_ids == ["s1"] and first.note is not None
    assert first.note.startswith(FIRST_OBSERVATION)
    assert first.note.endswith("Land clearing begins for both Building 1 & Building 2")
    assert items(fix5, "Vantage TX1") == []


def test_a_tdlr_registration_without_an_entry_is_held_for_review(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    def no_report(entries: dict[str, Any]) -> None:
        del entries["Microsoft SAT14"]["first_report"]

    result = run(make_test_context, write_overrides(tmp_path, counties, edit=no_report))
    sat14 = records(result)["Microsoft SAT14"]
    assert dates(sat14)["first_reported"] == "2022-02-18"  # Epoch's first observation
    assert (first_event(sat14).note or "").startswith(FIRST_OBSERVATION)
    ((kind, reason),) = items(result, "Microsoft SAT14")
    assert kind == "unknown_status" and "Epoch AI's first observation" in reason
    item = next(i for i in result.review if i.external_id == "Microsoft SAT14")
    assert item.data["first_observation"] == "2022-02-18"
    assert item.data["sources"] == [
        TDLR + "TABS2021019142",
        TDLR + "TABS2021019471",
        TDLR + "TABS2021019588",
        TDLR + "TABS2021019686",
    ]


def test_a_note_dates_the_announcement_of_its_site_and_of_another(fix5: ImportResult) -> None:
    # Epoch's Lordstown projection: "OpenAI stated Lordstown and Milam County could scale to
    # 1.5 GW within 18 months of their September 23, 2025 announcement". It dates both sites'
    # first report, before their land clearing (2025-10-12 and 2025-09-27).
    for name, start in (
        ("OpenAI Stargate Lordstown", "2025-10-12"),
        ("OpenAI Stargate Milam", "2025-09-27"),
    ):
        rec = records(fix5)[name]
        assert dates(rec) == {"first_reported": "2025-09-23", "construction_start": start}, name
        report = first_event(rec)
        assert (report.event, report.status, report.source_ids) == (
            "first_reported",
            "announced",
            ["s1"],
        )
        assert "their September 23, 2025 announcement" in (report.note or ""), name
    lordstown = Site("OpenAI Stargate Lordstown", "", "", "", "", "", None)
    milam = Site("OpenAI Stargate Milam", "", "", "", "", "", None)
    abilene = Site("OpenAI Stargate Abilene", "", "", "", "", "", None)
    text = "OpenAI stated Lordstown and Milam County could scale within 18 months of their "
    noted = noted_announcements(
        [lordstown, milam, abilene],
        {lordstown.name: [row("2027-03-01", text + "September 23, 2025 announcement.")]},
    )
    assert sorted(noted) == [lordstown.name, milam.name]  # Abilene is not named


def test_an_entry_quote_dates_an_announcement(fix5: ImportResult) -> None:
    # Meta's Hyperion page, the location source: "In December 2024, Meta announced that we are
    # building our largest data center to date in Richland Parish, Louisiana." Epoch's first row
    # (land clearing) is 2024-12-20; the month comes first.
    hyperion = records(fix5)["Meta Hyperion"]
    assert dates(hyperion) == {"first_reported": "2024-12", "construction_start": "2024-12-20"}
    report = first_event(hyperion)
    assert report.as_of.precision == "month" and report.source_ids == ["s2"]
    assert source(hyperion, "s2").supports == ["/location", "/status_history"]


def test_a_date_in_a_selected_source_link_is_held_for_review(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # A Selected Source whose link text or URL states a date earlier than Epoch's first row, at a
    # site without a first_report entry: the observation stays first_reported and a reviewer
    # checks the source.
    link = "- [Announcement (Jun 3, 2019)](https://example.org/news/2019/06/03/plans/)\n"
    edited = tmp_path / "edited.zip"
    with zipfile.ZipFile(CASES_ZIP) as src, zipfile.ZipFile(edited, "w") as dst:
        for member in src.namelist():
            data = src.read(member)
            if member == "data_centers.csv":
                reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
                out = io.StringIO()
                writer = csv.DictWriter(
                    out, fieldnames=reader.fieldnames or [], lineterminator="\n"
                )
                writer.writeheader()
                for r in reader:
                    if r["Name"] == "Google Bristow":
                        r["Selected Sources"] = link + r["Selected Sources"]
                    writer.writerow(r)
                data = out.getvalue().encode("utf-8")
            dst.writestr(member, data)
    result = run(make_test_context, write_overrides(tmp_path, counties), edited)
    item = next(i for i in result.review if i.external_id == "Google Bristow")
    assert item.kind == "unknown_status"
    assert item.data["sources"] == ["https://example.org/news/2019/06/03/plans/"]
    assert selected_date("Fortune (Jan 24, 2025)", "https://example.org/a") == date(2025, 1, 24)
    assert selected_date("x", "https://example.org/2024/03/hello/") == date(2024, 3, 1)
    assert selected_date("x", "https://example.org/report-12062024684954.pdf") is None


def test_a_first_report_whose_status_is_ahead_of_the_first_event_is_held(
    fix5: ImportResult,
) -> None:
    # CoreWeave Lancaster: PA DEP received an air permit application (proposed) on 2025-05-14,
    # before CoreWeave's announcement (Epoch's first row, 2025-07-15): the application dates
    # nothing, and a reviewer decides.
    rec = records(fix5)["CoreWeave Lancaster Greenfield site"]
    assert dates(rec) == {"announced": "2025-07-15", "first_reported": "2025-07-15"}
    (item,) = [i for i in fix5.review if i.external_id == "CoreWeave Lancaster Greenfield site"]
    assert item.kind == "conflict" and item.data["first_report_status"] == "proposed"


# ---------------------------------------------------------------------------- capacity


def test_a_cited_capacity_replaces_epochs_model(fix5: ImportResult) -> None:
    # CoreWeave Ellendale ND: Applied Digital (2026-07-01, a Selected Source) has 175 MW of
    # critical IT capacity live; Epoch's columns say 68 MW IT and 88 MW, its model.
    ellendale = records(fix5)["CoreWeave Ellendale ND"]
    (phase,) = ellendale.phases
    assert (phase.capacity.it_mw, phase.capacity.facility_mw, phase.capacity.mw_as_stated) == (
        175.0,
        None,
        "175 MW now live",
    )
    release = source(ellendale, phase.source_ids[1])
    assert release.publisher == "Applied Digital" and release.quote is not None
    meta = ellendale.field_meta["/phases/0/capacity"]
    assert meta.method == "stated" and meta.source_ids == [release.id]
    assert [c["value"] for c in meta.conflicts] == [{"it_mw": 68.0, "facility_mw": 88.0}]
    assert [i.kind for i in fix5.review if i.external_id == "CoreWeave Ellendale ND"] == [
        "conflict"  # the bitcoin miner's site (Applied Digital), not the capacity
    ]
    assert all("stated" not in i.data for i in fix5.review)
    # xAI QTS Atlanta: Business Insider's 20 MW of total power, not Epoch's 24 MW (17.3 MW of IT
    # power at its PUE).
    (xai,) = records(fix5)["xAI QTS Atlanta"].phases
    assert (xai.capacity.it_mw, xai.capacity.facility_mw) == (None, 20.0)


def test_epochs_model_against_the_mw_its_note_states_is_held(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # Epoch's 2025-11-24 note, which set the 68/88 MW columns: "bringing the first building to its
    # expected 100 MW". Without a cited capacity, the phase keeps only that phrase.
    def no_capacity(entries: dict[str, Any]) -> None:
        del entries["CoreWeave Ellendale ND"]["capacity"]

    result = run(make_test_context, write_overrides(tmp_path, counties, edit=no_capacity))
    (phase,) = records(result)["CoreWeave Ellendale ND"].phases
    assert (phase.capacity.it_mw, phase.capacity.facility_mw) == (None, None)
    assert phase.capacity.mw_as_stated == "bringing the first building to its expected 100 MW"
    item = next(
        i for i in result.review if i.external_id == "CoreWeave Ellendale ND" and "stated" in i.data
    )
    assert item.kind == "conflict" and item.data["epoch"] == {"it_mw": 68.0, "facility_mw": 88.0}
    # A hedged figure, another facility's or one that agrees is no conflict.
    assert stated_capacity("The site is estimated to have reached at least ~830 MW.") is None
    assert stated_capacity("The plant reached 138.8 MW of nameplate turbine capacity.") is None
    assert stated_capacity("Building 1 reached 50 MW. Next row.") == (50.0, "reached 50 MW")


# ---------------------------------------------------------------------------- operation


def test_a_note_can_date_operation_before_its_row(fix5: ImportResult) -> None:
    # Microsoft-Nebius New Jersey, 2026-04-15: "We estimate Building 1 became operational around
    # early 2026 ... SemiAnalysis estimated the full 50 MW was reached around January to February
    # 2026": the first quarter, not the month of the confirming image.
    nebius = records(fix5)["Microsoft-Nebius New Jersey"]
    assert nebius.dates["operating_since"].model_dump() == {
        "value": "2026-Q1",
        "precision": "quarter",
    }
    energized = next(e for e in nebius.status_history if e.event == "energized")
    assert energized.note == (
        "We estimate Building 1 became operational around early 2026 … SemiAnalysis estimated "
        "the full 50 MW was reached around January to February 2026"
    )
    assert items(fix5, "Microsoft-Nebius New Jersey") == []


def test_operation_dated_in_words_the_importer_cannot_read_dates_nothing() -> None:
    rows = [
        row("2024-01-02", "Land clearing begins."),
        row("2025-03-14", "Building 1 operational. We think it became operational a while ago.", 1),
    ]
    events = status_events(rows, TODAY)
    assert [e["event"] for e in events] == ["construction_start", "other"]
    assert operation_review(rows, TODAY) == ("2025-03-14", "We think it became operational")
    # A period the row precedes, or one before the previous event, is not read either.
    assert operation_period("It became operational in March 2026.", date(2026, 2, 1)) == (
        None,
        ["It became operational in March 2026"],
    )
    early = parse_period("early 2026")
    assert early is not None and early.as_of == {"value": "2026", "precision": "year"}
    assert operation_period("It came online in Q3 2025.", date(2025, 10, 1))[0].as_of == {  # type: ignore[index,union-attr]
        "value": "2025-Q3",
        "precision": "quarter",
    }


# ---------------------------------------------------------------------------- sources


def test_a_location_conflict_among_the_cited_sources_is_recorded(fix5: ImportResult) -> None:
    # Microsoft SAT40: the entry places the campus in Bexar County from TDLR's SAT40 record; TDLR's
    # SAT11-14 record, Epoch's other Selected Source for 15000 Lambda Drive, says Medina.
    sat40 = records(fix5)["Microsoft SAT40"]
    assert sat40.location.county_fips == "48029"
    (conflict,) = sat40.field_meta["/location"].conflicts
    assert conflict["value"] == "Medina County, TX"
    assert isinstance(conflict["source_ids"], list)
    other = source(sat40, str(conflict["source_ids"][0]))
    assert str(other.url) == TDLR + "EABPRJB8824650" and other.supports == []
    assert other.quote is not None and "Location County: Medina" in other.quote
    ((kind, reason),) = items(fix5, "Microsoft SAT40")
    assert kind == "conflict" and "disagree on its location" in reason


def test_a_cited_source_that_states_another_status_is_held_for_review(
    fix5: ImportResult, make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # Meta Montgomery's location source until now, Baxtel's campus page, listed the campus as
    # "Under Construction" while the record is operating; the committed source states no status.
    montgomery = records(fix5)["Meta Montgomery"]
    assert montgomery.status == "operating"
    assert str(montgomery.sources[1].url) == "https://baxtel.com/news/4870"
    assert all("states the facility" not in r for _, r in items(fix5, "Meta Montgomery"))

    def contrary(entries: dict[str, Any]) -> None:
        entries["Meta Montgomery"]["states_status"] = "under_construction"

    result = run(make_test_context, write_overrides(tmp_path, counties, edit=contrary))
    (item,) = [
        i
        for i in result.review
        if i.external_id == "Meta Montgomery" and "states the facility" in i.reason
    ]
    assert item.kind == "conflict"
    assert (item.data["states_status"], item.data["status"]) == ("under_construction", "operating")


def test_notes_keep_whole_urls_or_none_and_cite_the_sources_they_name(fix5: ImportResult) -> None:
    # Chester's first note ends "Source: https://www.datacenterdynamics.com/en/news/chirisa-...",
    # Dalton's second "Source: https://investors.corescientific.com/...": cut at 200 characters
    # they ended in half a URL.
    chester = records(fix5)["CoreWeave Chester VA"].status_history[0]
    assert chester.note is not None and chester.note.endswith("Source: datacenterdynamics.com")
    dalton = records(fix5)["CoreWeave Dalton 1 & 2"].status_history[1]
    assert dalton.note is not None and dalton.note.endswith("Source: investors.corescientific.com")
    for rec in records(fix5).values():
        for e in rec.status_history:
            assert "http" not in (e.note or ""), (rec.canonical_name, e.note)
    long = "x" * 170 + " Source: https://example.org/a/very/long/path/that/would/be/cut"
    assert event_note(long) == "x" * 170 + " Source: example.org"
    assert strip_urls("see https://www.example.org/x.") == "see example.org"
    # SemiAnalysis is Microsoft-Nebius New Jersey's sixth Selected Source, past the five-link
    # cap, and its 2026-04-15 note quotes SemiAnalysis: it is cited.
    nebius = records(fix5)["Microsoft-Nebius New Jersey"]
    links = [str(s.url) for s in nebius.sources if s.title and s.id != "s1"]
    assert len(links) == 6
    assert links[-1].startswith("https://newsletter.semianalysis.com/p/stop-saying-half-of-2026")


# ---------------------------------------------------------------------------- milestones


def test_cited_milestones_date_construction_and_operation(
    fix5: ImportResult, make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    # Google Bristow: "one data center ... which became operational in 2023" (Prince William
    # Times); Epoch's imagery dates Building 1 to 2024-04-29, which becomes an observation.
    bristow = records(fix5)["Google Bristow"]
    assert bristow.dates["operating_since"].model_dump() == {"value": "2023", "precision": "year"}
    epoch_row = next(e for e in bristow.status_history if e.as_of.value == "2024-04-29")
    assert epoch_row.event == "other"
    # OpenAI Stargate Wisconsin: Vantage broke ground on Wednesday, 2025-12-17 (The Daily
    # Reporter); Epoch's "Ground broken" row is 2026-01-02. Vantage's announcement of 2025-10-22
    # is the first report.
    wis = records(fix5)["OpenAI Stargate Wisconsin"]
    assert dates(wis) == {"first_reported": "2025-10-22", "construction_start": "2025-12-17"}

    def too_early(entries: dict[str, Any]) -> None:
        entries["Google Bristow"]["milestones"][0]["as_of"] = "2020"

    result = run(make_test_context, write_overrides(tmp_path, counties, edit=too_early))
    rec = records(result)["Google Bristow"]
    assert dates(rec)["operating_since"] == "2024-04-29"  # not applied: before construction
    (item,) = [i for i in result.review if i.external_id == "Google Bristow"]
    assert item.kind == "conflict" and "earlier than an event of an earlier status" in item.reason
