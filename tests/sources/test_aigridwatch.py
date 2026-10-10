"""The AI GridWatch importer (atlas/sources/aigridwatch.py) against the committed fixture."""

from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas.cli import main
from atlas.crosswalk import from_aigridwatch_stage
from atlas.geo.counties import CountyIndex
from atlas.geo.places import PlaceIndex
from atlas.geocode import Gazetteer
from atlas.net import FetchError, fetch, make_client
from atlas.schema.record import FacilityRecord
from atlas.sources.aigridwatch import (
    IMPORTER,
    OVERRIDES_PATH,
    PROJECTS_URL,
    PartyName,
    filing_names,
    has_filing,
    late_announcement,
    looks_like_person,
    looks_like_person_name,
    milestone_events,
    org_key,
    orgs_overlap,
    parse_locality,
    party_name,
    resolve_county,
)
from atlas.sources.base import (
    Candidate,
    ImportContext,
    ImportResult,
    ReviewItem,
    apply_import,
    read_review_queue,
)
from atlas.store import RecordStore
from atlas.validate import validate_record

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch" / "projects.json"
MakeContext = Callable[..., ImportContext]
SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "aigridwatch"


@pytest.fixture(scope="module")
def agw_places() -> Iterator[PlaceIndex]:
    """The place polygons the AI GridWatch fixtures' points lie in (fixtures/aigridwatch/README)."""
    index = PlaceIndex.load(SAMPLES / "places" / "agw_places.zip", verify_sha256=False)
    yield index
    index.close()


@pytest.fixture
def make_test_context(make_test_context: MakeContext, agw_places: PlaceIndex) -> MakeContext:
    """The shared factory, with the AI GridWatch samples of the place polygons and the county
    subdivisions (the shared samples have few of the fixtures' places)."""
    gazetteer = Gazetteer.load(
        cousub_zip=SAMPLES / "gazetteer" / "agw_cousubs.zip", verify_sha256=False
    )

    def factory(**overrides: Any) -> ImportContext:
        overrides.setdefault("places", agw_places)
        overrides.setdefault("gazetteer", gazetteer)
        return make_test_context(**overrides)

    return factory


# The fixture's project for each stage, and what the crosswalk makes of it.
EXPECTED = {
    "stokes-county-project-delta": ("In review", "proposed", None, "reported"),
    "qts-richmond-3": ("Under construction", "under_construction", None, "reported"),
    "google-bristow": ("Operating", "operating", None, "reported"),
    "archbald-i-llc-archbald-ii-llc-unnamed-data-centers-archbald-borough-pa": (
        "Rumored",
        "announced",
        None,
        "rumor",
    ),
    "red-oak-compass-campus": ("Approved", "permitted", None, "reported"),
    "sabey-decatur-township-in": ("Approved", "permitted", None, "reported"),
    "burkhalter-road-statesboro-ga": ("Awaiting decision", "proposed", None, "reported"),
    # "the rezoning was judicially voided": a court ruling ended it, not the developer.
    "pw-digital-gateway-va": ("Withdrawn", "cancelled", "litigation", "reported"),
    "talen-montour-pa": ("Denied", "denied", "local_denial", "reported"),
    "meta-leap-lebanon-in": ("Proposed", "announced", None, "reported"),
    "abei-energy-data-center-starke-in": ("Blocked by ban", "paused", "moratorium", "reported"),
}


# Rows of the fixture without as_of (sabey-decatur-township-in, talen-montour-pa): no milestone
# reaches their stage, so their stage is an observation at the file's generated date (2026-10-07).
# Tests about other things date them through dated().
UNDATED = ("sabey-decatur-township-in", "talen-montour-pa")


def load_fixture() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return doc


def dated(doc: dict[str, Any]) -> None:
    """Give the fixture's rows without as_of the file's date, as if AI GridWatch had read them."""
    for p in doc["projects"]:
        if not p["as_of"]:
            p["as_of"] = "2026-10-07"


def project(doc: dict[str, Any], pid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(p for p in doc["projects"] if p["id"] == pid)
    return found


def run(
    make_test_context: MakeContext,
    path: Path = FIXTURE,
    *,
    without_epoch: bool = True,
    **ctx: Any,
) -> ImportResult:
    args = argparse.Namespace(without_epoch=without_epoch)
    return IMPORTER.run(make_test_context(input_path=path, **ctx), args)


def run_edited(
    make_test_context: MakeContext,
    tmp_path: Path,
    edit: Callable[[dict[str, Any]], None],
    *,
    without_epoch: bool = True,
    **ctx: Any,
) -> ImportResult:
    doc = copy.deepcopy(load_fixture())
    edit(doc)
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return run(make_test_context, path, without_epoch=without_epoch, **ctx)


def records(result: ImportResult) -> dict[str, FacilityRecord]:
    return {c.match_values[0]: c.record for c in result.candidates}


def gazetteer_place(state: str, name: str) -> tuple[float, float]:
    from atlas.geocode import Gazetteer

    found = Gazetteer.load().place(state, name)
    assert found is not None, name
    return found[0], found[1]


def snapshot(*roots: Path) -> dict[str, bytes]:
    return {
        str(p): p.read_bytes() for root in roots for p in sorted(root.rglob("*")) if p.is_file()
    }


# ---------------------------------------------------------------------------- parsing helpers


@pytest.mark.parametrize(
    ("text", "state", "place", "counties", "hint"),
    [
        ("Muncy Township (Lycoming County)", "PA", "Muncy Township", ("Lycoming County",), None),
        ("Hood County (Granbury area)", "TX", None, ("Hood County",), "Granbury"),
        (
            "Archbald Borough (Lackawanna County)",
            "PA",
            "Archbald Borough",
            ("Lackawanna County",),
            None,
        ),
        ("North Slope Borough", "AK", None, ("North Slope Borough",), None),
        ("Caddo Parish", "LA", None, ("Caddo Parish",), None),
        ("Mercer County / Burgin", "KY", "Burgin", ("Mercer County",), None),
        (
            "San Marcos (Hays/Guadalupe Counties)",
            "TX",
            "San Marcos",
            ("Hays County", "Guadalupe County"),
            None,
        ),
        (
            "East Coventry / Lower Pottsgrove / Limerick Townships (Chester / Montgomery County)",
            "PA",
            "East Coventry / Lower Pottsgrove / Limerick Townships",
            ("Chester County", "Montgomery County"),
            None,
        ),
        ("City of Lancaster (Lancaster County)", "PA", "Lancaster", ("Lancaster County",), None),
        ("Globeville-Elyria-Swansea (Denver)", "CO", "Globeville-Elyria-Swansea", (), "Denver"),
        ("Storey County (near Reno)", "NV", None, ("Storey County",), "Reno"),
        (
            "Shawnee County (Wakarusa area, near Topeka)",
            "KS",
            None,
            ("Shawnee County",),
            "Wakarusa",
        ),
        ("Central Ohio", "OH", "Central Ohio", (), None),
    ],
)
def test_parse_locality(
    text: str, state: str, place: str | None, counties: tuple[str, ...], hint: str | None
) -> None:
    got = parse_locality(text, state)
    assert (got.place, got.county_texts, got.hint) == (place, counties, hint)


def test_nearby_hints() -> None:
    assert parse_locality("Storey County (near Reno)", "NV").hint_is_nearby
    assert parse_locality("Hood County (Granbury area)", "TX").hint_is_nearby
    assert not parse_locality("Decatur Township (Indianapolis)", "IN").hint_is_nearby
    assert not parse_locality("Globeville-Elyria-Swansea (Denver)", "CO").hint_is_nearby


def test_resolve_county_picks_the_one_containing_the_point(counties: CountyIndex) -> None:
    two = parse_locality("Somewhere (Chester / Montgomery County)", "PA")
    chester = counties.by_name("PA", "Chester County")
    assert chester is not None
    lat, lon = counties.centroid(chester.fips)
    assert resolve_county(two, "PA", counties, (lat, lon)) == chester
    assert resolve_county(two, "PA", counties, None) is None
    one = parse_locality("Muncy Township (Lycoming County)", "PA")
    lycoming = resolve_county(one, "PA", counties, None)
    assert lycoming is not None and lycoming.name == "Lycoming"
    assert (
        resolve_county(parse_locality("Palmetto (South Fulton County)", "GA"), "GA", counties, None)
        is None
    )
    # One named county and a point outside it: no county, rather than a county the point is not in.
    montgomery = counties.by_name("PA", "Montgomery County")
    assert montgomery is not None
    assert resolve_county(one, "PA", counties, counties.centroid(montgomery.fips)) is None


def test_party_helpers() -> None:
    assert looks_like_person("Jane Example (developer)")
    assert not looks_like_person("Example Holdings LLC (developer)")
    assert not looks_like_person("Constellation Energy Group (parcels)")
    assert not looks_like_person("Meta")
    assert looks_like_person_name("Jane Example") and looks_like_person_name("Jane O'Example")
    assert looks_like_person_name("Jane Q. Example")
    assert not looks_like_person_name("Scale Microgrids")
    assert not looks_like_person_name("AWS") and not looks_like_person_name("UK")
    assert party_name("Example Ventures (Jane Example)") == PartyName("Example Ventures", True)
    assert party_name("Jane Example (developer)") == PartyName(None, True)
    assert party_name("Amazon (AWS)") == PartyName("Amazon")
    assert party_name("Constellation Energy Group (parcels)") == PartyName(
        "Constellation Energy Group"
    )
    assert party_name("Example Institute of Technology (proposed site)") == PartyName(
        "Example Institute of Technology"
    )
    assert party_name("67 MW") == PartyName(None, capacity=True)
    assert party_name("1.2 GW") == PartyName(None, capacity=True)
    assert party_name("Hut 8") == PartyName("Hut 8")
    assert filing_names("Archbald I LLC / Archbald II LLC") == ["Archbald I LLC", "Archbald II LLC"]
    assert filing_names("4AM Development, LLC") == ["4AM Development, LLC"]
    assert filing_names("A One LLC; applicant changed to B Two LLC in July 2025") == ["A One LLC"]


# ---------------------------------------------------------------------------- stages and events


def test_every_stage_maps_through_the_crosswalk(
    make_test_context: MakeContext, counties: CountyIndex, today: date, tmp_path: Path
) -> None:
    result = run_edited(make_test_context, tmp_path, dated)
    got = records(result)
    assert sorted(got) == sorted(EXPECTED)
    doc = load_fixture()
    for pid, (stage, status, reason, evidence) in EXPECTED.items():
        rec = got[pid]
        assert project(doc, pid)["stage"] == stage
        assert (rec.status, rec.status_reason, rec.evidence_level) == (status, reason, evidence), (
            pid
        )
        cw = from_aigridwatch_stage(stage, has_filing=has_filing(project(doc, pid)))
        assert rec.status == cw.status
        assert validate_record(rec, counties=counties, today=today) == [], pid
    assert {project(doc, pid)["stage"] for pid in got} == {
        "In review",
        "Under construction",
        "Operating",
        "Rumored",
        "Approved",
        "Awaiting decision",
        "Withdrawn",
        "Denied",
        "Proposed",
        "Blocked by ban",
    }
    (snap,) = result.inputs
    assert snap.upstream_version == "2026-10-07" and snap.license == "CC-BY-4.0"
    assert result.metrics["projects"] == 12 and result.metrics["upstream_events_unused"] == 30


def test_a_stage_without_a_date_is_an_observation_that_dates_nothing(
    make_test_context: MakeContext,
) -> None:
    # Sabey (Approved) and Talen (Denied) have no as_of and no milestone date that reaches their
    # stage. Owner decision of 2026-10-09: the stage is published as an `other` observation at the
    # file's generated date (2026-10-07), which sets the status and dates nothing, as an OSM tag
    # does; the rows are no longer held.
    result = run(make_test_context)
    got = records(result)
    assert set(UNDATED) <= set(got) and len(got) == len(EXPECTED)
    assert not [i for i in result.review if i.kind == "unknown_status" and "stage" in i.data]
    assert result.metrics["held_for_review"] == 0
    assert result.metrics["stage_events_undated"] == 2
    for pid in UNDATED:
        rec = got[pid]
        assert rec.status == EXPECTED[pid][1]
        stage = rec.status_history[-1]
        assert (stage.event, stage.status, stage.as_of.value, stage.planned) == (
            "other",
            EXPECTED[pid][1],
            "2026-10-07",
            False,
        )
        assert stage.note == (
            f"AI GridWatch stage '{EXPECTED[pid][0]}', seen in its file on the file's date: the "
            "row has no as_of, so this is not the date the stage began"
        )
        # The observation dates nothing (Sabey's and Talen's first report is their earliest
        # event-log entry); no decision date exists.
        assert all(d.value != "2026-10-07" for d in rec.dates.values()), pid
        assert {"approved", "cancelled"}.isdisjoint(rec.dates), pid
    # A row with an as_of is dated by it, never by the file.
    assert all(
        e.note is None or "as of 2026-10-07" not in e.note
        for pid, rec in got.items()
        if pid not in UNDATED
        for e in rec.status_history
    )


def test_an_undated_stage_keeps_the_date_it_was_first_seen(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    # A later file (generated 2026-10-14) still shows Sabey's stage: the observation keeps the
    # date of the file that first showed it, so a weekly run rewrites nothing.
    first = records(run(make_test_context))["sabey-decatur-township-in"]
    stored = first.model_copy(update={"id": "gwa-00000001"})

    def later(doc: dict[str, Any]) -> None:
        doc["generated"] = "2026-10-14"

    again = run_edited(make_test_context, tmp_path, later, records={stored.id: stored})
    rec = records(again)["sabey-decatur-township-in"]
    assert rec.status_history[-1].as_of.value == "2026-10-07"
    assert rec.status_history == first.status_history

    # A stage that changed is a new observation, at the new file's date.
    def changed(doc: dict[str, Any]) -> None:
        doc["generated"] = "2026-10-14"
        project(doc, "sabey-decatur-township-in")["stage"] = "Under construction"

    moved = run_edited(make_test_context, tmp_path, changed, records={stored.id: stored})
    other = records(moved)["sabey-decatur-township-in"].status_history[-1]
    assert (other.status, other.as_of.value) == ("under_construction", "2026-10-14")


def test_proposed_splits_on_a_filing(make_test_context: MakeContext, tmp_path: Path) -> None:
    pid = "meta-leap-lebanon-in"

    def add_filing(doc: dict[str, Any]) -> None:
        project(doc, pid)["rezoning_filed"] = "2026-03-01"

    rec = records(run_edited(make_test_context, tmp_path, add_filing))[pid]
    assert rec.status == "proposed"
    # A date on the 1st of a month is AI GridWatch's way of writing the month. The log's entry of
    # 2024-11-25 ("Meta proposes ~$800 million data center in Lebanon") reports the project before
    # AI GridWatch's announced date, so it dates the first report (n31).
    assert [(e.event, e.as_of.value, e.as_of.precision) for e in rec.status_history] == [
        ("first_reported", "2024-11-25", "day"),
        ("announced", "2026-02-11", "day"),
        ("application_filed", "2026-03", "month"),
    ]

    def filing_event(doc: dict[str, Any]) -> None:
        project(doc, pid)["events"].append(
            {
                "date": "2026-03-01",
                "kind": "filing",
                "summary": "The developer filed a rezoning application.",
                "source": "",
            }
        )

    rec = records(run_edited(make_test_context, tmp_path, filing_event))[pid]
    assert rec.status == "proposed"
    assert rec.status_history[-1].event == "other"  # the stage's status, with no dated filing


def test_milestones_that_agree_with_the_stage_add_no_event(make_test_context: MakeContext) -> None:
    got = records(run(make_test_context))
    red_oak = got["red-oak-compass-campus"]
    # AI GridWatch's decided_date is May 12, but the row says the council voted "around midnight
    # following the May 11 meeting": the vote is dated by its meeting, so the May 11 hearing was
    # held that day (n37). The first report is the event log's earliest entry about this project,
    # the 830-acre rezoning's P&Z vote: the 2020 entry is Compass's first Red Oak campus, 225
    # acres (n24).
    assert [
        (e.seq, e.event, e.status, e.as_of.value, e.planned) for e in red_oak.status_history
    ] == [
        (1, "first_reported", "announced", "2026-04-27", False),
        (2, "hearing_held", "proposed", "2026-05-11", False),
        (3, "approved", "permitted", "2026-05-11", False),
    ]
    assert red_oak.dates["approved"].value == "2026-05-11"
    assert red_oak.dates["first_reported"].value == "2026-04-27"
    pw = got["pw-digital-gateway-va"]
    assert [(e.event, e.status) for e in pw.status_history] == [
        ("first_reported", "announced"),
        ("withdrawn", "cancelled"),
    ]
    # decided_date 2026-04-01 is April 2026; the event log says "(April 2026)".
    assert (pw.dates["cancelled"].value, pw.dates["cancelled"].precision) == ("2026-04", "month")
    assert all(e.source_ids == ["s1"] for e in pw.status_history)


def test_a_stage_the_milestones_do_not_reach_adds_an_other_event(
    make_test_context: MakeContext,
) -> None:
    abei = records(run(make_test_context))["abei-energy-data-center-starke-in"]
    *milestones, other = abei.status_history
    # The 2025-11-12 hearing is not imported: no decision or event of that day says it was held.
    # Nor is rezoning_filed: the note says Abei only asked about rezoning (n40).
    assert [e.event for e in milestones] == ["announced"]
    assert (other.event, other.status, other.as_of.value) == ("other", "paused", "2026-09-02")
    assert other.note == "AI GridWatch stage 'Blocked by ban' as of 2026-09-02"
    assert abei.status == "paused" and abei.status_reason == "moratorium"


def test_the_other_event_is_never_older_than_the_milestones(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    pid = "stokes-county-project-delta"

    def stale_as_of(doc: dict[str, Any]) -> None:
        p = project(doc, pid)
        p["stage"], p["as_of"] = "Approved", "2026-01-01"  # older than the 2026-07-20 filing

    rec = records(run_edited(make_test_context, tmp_path, stale_as_of))[pid]
    other = rec.status_history[-1]
    assert (other.event, other.as_of.value, rec.status) == ("other", "2026-07-20", "permitted")
    assert other.note == "AI GridWatch stage 'Approved' as of 2026-01-01"


def test_an_announcement_after_a_filing_is_left_out_and_flagged(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    pid = "meta-leap-lebanon-in"  # announced 2026-02-11

    def late(doc: dict[str, Any]) -> None:
        project(doc, pid)["rezoning_filed"] = "2026-01-15"

    assert late_announcement(project(load_fixture(), pid)) is None
    result = run_edited(make_test_context, tmp_path, late)
    rec = records(result)[pid]
    # Not proposed (filed) -> announced -> proposed: the late announcement is dropped. Without
    # AI GridWatch's announced date, the event log's earliest entry dates the first report.
    assert [(e.event, e.status, e.as_of.value) for e in rec.status_history] == [
        ("first_reported", "announced", "2024-11-25"),
        ("application_filed", "proposed", "2026-01-15"),
    ]
    assert "announced" not in rec.dates and rec.dates["first_reported"].value == "2024-11-25"
    (item,) = [i for i in result.review if i.kind == "conflict" and i.external_id == pid]
    assert item.data == {"announced": "2026-02-11", "rezoning_filed": "2026-01-15"}


def test_a_stage_observation_sets_no_derived_date(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    def withdrawn_undated(doc: dict[str, Any]) -> None:
        project(doc, "pw-digital-gateway-va")["decided_date"] = ""

    got = records(run_edited(make_test_context, tmp_path, withdrawn_undated))
    bristow = got["google-bristow"]  # Operating, with no milestone dates
    assert [e.event for e in bristow.status_history] == ["other"] and bristow.status == "operating"
    # The "other" event is dated when AI GridWatch read its source, not when the site opened.
    assert "operating_since" not in bristow.dates and "first_reported" not in bristow.dates
    pw = got["pw-digital-gateway-va"]  # Withdrawn, no decision date
    assert pw.status == "cancelled" and "cancelled" not in pw.dates


def test_hearing_planned_before_its_date_and_held_after(
    make_test_context: MakeContext,
) -> None:
    doc = load_fixture()
    red_oak = project(doc, "red-oak-compass-campus")
    before = milestone_events(red_oak, date(2026, 5, 1))
    assert [(e["event"], e["planned"]) for e in before] == [
        ("hearing_scheduled", True),
        ("approved", True),
    ]
    # Once the date has passed, a hearing is held only when the row says so (see
    # test_a_past_hearing_is_held_only_when_the_row_says_so).
    after = milestone_events(red_oak, date(2026, 10, 12))
    # The decision is dated by the May 11 meeting the row says it ended (n37), so it is held.
    assert [(e["event"], e["planned"]) for e in after] == [
        ("hearing_held", False),
        ("approved", False),
    ]
    # Run as of 2026-05-01: the planned events do not set the status; the stage does.
    early = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    rec = records(run(make_test_context, now=early, today=early.date()))["red-oak-compass-campus"]
    assert [(e.event, e.planned) for e in rec.status_history] == [
        ("first_reported", False),
        ("hearing_scheduled", True),
        ("approved", True),
        ("other", False),
    ]
    assert rec.status == "permitted"


def test_control_characters_in_a_row_are_dropped(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    # One stray BEL or ESC upstream must not hold a record: clean_text is the shared cleaner.
    def edit(doc: dict[str, Any]) -> None:
        row = project(doc, "qts-richmond-3")
        row["name"] = "QTS\x07 Richmond 3"
        row["locality"] = "Sandston\x1b (Henrico County)"

    plain = records(run(make_test_context))["qts-richmond-3"]
    result = run_edited(make_test_context, tmp_path, edit)
    assert not [i for i in result.review if i.kind == "invalid"]
    got = records(result)["qts-richmond-3"]
    assert got.canonical_name == plain.canonical_name == "QTS Richmond 3 (Sandston, VA)"
    assert got.location == plain.location


def test_unverified_rows_are_skipped(make_test_context: MakeContext) -> None:
    result = run(make_test_context)
    assert "project-marvel-bessemer-al" not in records(result)
    (item,) = [i for i in result.review if i.external_id == "project-marvel-bessemer-al"]
    assert item.kind == "unverified_upstream" and item.source == "aigridwatch"
    assert result.metrics["unverified"] == 1


def test_unknown_stage_and_bad_rows(make_test_context: MakeContext, tmp_path: Path) -> None:
    def edit(doc: dict[str, Any]) -> None:
        project(doc, "qts-richmond-3")["stage"] = "Mothballed"
        project(doc, "google-bristow")["state"] = "PR"
        del project(doc, "talen-montour-pa")["locality"]

    result = run_edited(make_test_context, tmp_path, edit)
    kinds = {(i.kind, i.external_id) for i in result.review}
    assert ("unknown_status", "qts-richmond-3") in kinds
    assert ("out_of_scope", "google-bristow") in kinds
    assert ("invalid", None) in kinds
    assert len(result.candidates) == 8  # 11 verified rows, 3 edited out


# ---------------------------------------------------------------------------- location and parties


def test_location_and_county_from_the_locality(
    make_test_context: MakeContext, counties: CountyIndex
) -> None:
    got = records(run(make_test_context))
    archbald = got[
        "archbald-i-llc-archbald-ii-llc-unnamed-data-centers-archbald-borough-pa"
    ].location
    lackawanna = counties.by_name("PA", "Lackawanna County")
    assert lackawanna is not None
    assert (archbald.precision, archbald.geocode_method, archbald.municipality, archbald.city) == (
        "locality",
        "source_coords",
        "Archbald Borough",
        None,
    )
    assert (archbald.county_fips, archbald.county_name) == (lackawanna.fips, "Lackawanna")
    assert (archbald.lat, archbald.lon) == (41.52458, -75.553656)
    stokes = got["stokes-county-project-delta"]
    # The row's log puts the 1,800 acres "near Walnut Cove": the town is not named, and its point
    # tells only the county (n32).
    assert stokes.canonical_name == "Project Delta (Stokes County, NC)"
    assert stokes.location.city is None and stokes.location.county_name == "Stokes"
    assert stokes.location.precision == "county"
    pw = got["pw-digital-gateway-va"]
    assert pw.canonical_name == "PW Digital Gateway (Prince William County, VA)"
    assert pw.location.city is None and pw.location.county_name == "Prince William"


def test_rows_without_coordinates_are_geocoded(
    make_test_context: MakeContext, counties: CountyIndex, tmp_path: Path
) -> None:
    got = records(run_edited(make_test_context, tmp_path, dated))
    talen = got["talen-montour-pa"].location
    assert (talen.precision, talen.geocode_method, talen.county_name) == (
        "county",
        "county_centroid",
        "Montour",
    )
    assert talen.county_fips is not None
    assert (talen.lat, talen.lon) == counties.centroid(talen.county_fips)
    sabey = got["sabey-decatur-township-in"]
    loc = sabey.location
    # "Decatur Township (Indianapolis)": a township is no Census place, but a county
    # subdivision, so the record is at its Gazetteer point and names it (not Indianapolis).
    assert (loc.precision, loc.geocode_method, loc.city, loc.municipality) == (
        "locality",
        "gazetteer",
        None,
        "Decatur Township",
    )
    assert sabey.field_meta["/location"].method == "derived"


def test_point_outside_the_state_is_a_county_mismatch(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    def move(doc: dict[str, Any]) -> None:
        project(doc, "google-bristow").update({"lat": 41.13, "lon": -104.8})  # Cheyenne, WY

    result = run_edited(make_test_context, tmp_path, move)
    assert "google-bristow" not in records(result)
    assert ("county_mismatch", "google-bristow") in {(i.kind, i.external_id) for i in result.review}


def test_parties_aliases_capacity_and_sources(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    got = records(run(make_test_context))
    archbald = got["archbald-i-llc-archbald-ii-llc-unnamed-data-centers-archbald-borough-pa"]
    assert [o.name for o in archbald.parties.filing_entities] == [
        "Archbald I LLC",
        "Archbald II LLC",
    ]
    assert [(a.name, a.kind) for a in archbald.aliases] == [
        ("Archbald I LLC", "filing_llc"),
        ("Archbald II LLC", "filing_llc"),
    ]
    # AI GridWatch's "Operator/developer" field is the developer: no operator is published.
    assert [o.name for o in got["pw-digital-gateway-va"].parties.developer] == [
        "Compass Datacenters",
        "QTS",
    ]
    assert not any(r.parties.operator for r in got.values())
    bristow = got["google-bristow"]
    assert [o.name for o in bristow.parties.tenant] == ["Google DeepMind"]
    # "Epoch AI estimates ~279 MW" states no basis: the figure is kept as stated, not as IT MW.
    assert bristow.capacity.mw_as_stated == "AI GridWatch size_mw: 279"
    assert bristow.capacity.it_mw is None and "/capacity/it_mw" not in bristow.field_meta
    assert got["pw-digital-gateway-va"].site.acreage == 2100.0
    s1, s2 = archbald.sources
    assert (str(s1.url), s1.publisher, s1.source_type, s1.license) == (
        "https://aigridwatch.com/projects",
        "AI GridWatch",
        "open_dataset",
        "CC-BY-4.0",
    )
    assert s1.supports == [
        "/canonical_name",
        "/aliases",
        "/parties",
        "/location",
        "/capacity",
        "/site",
        "/status_history",
    ]
    assert (s2.publisher, s2.source_type, s2.supports) == (
        "gis.dep.pa.gov",
        "government_record",
        [],
    )
    assert archbald.external_ids == {"aigridwatch_id": [archbald.external_ids["aigridwatch_id"][0]]}

    def edit(doc: dict[str, Any]) -> None:
        p = project(doc, "pw-digital-gateway-va")
        p["owner"] = "Jane Example (developer) / Example Land Holdings LLC"
        p["size_mw"] = 25000
        p["source"] = "not a url"

    result = run_edited(make_test_context, tmp_path, edit)
    pw = records(result)["pw-digital-gateway-va"]
    assert [o.name for o in pw.parties.owner] == ["Example Land Holdings LLC"]
    assert result.metrics["persons_dropped"] == 1
    stokes = records(run(make_test_context))["stokes-county-project-delta"]
    assert [o.name for o in stokes.parties.developer] == ["Engineered Land Solutions"]  # "(ELS)"
    assert pw.capacity.it_mw is None and len(pw.sources) == 1
    assert ("unit_parse", "pw-digital-gateway-va") in {
        (i.kind, i.external_id) for i in result.review
    }


def test_parties_hold_organizations_only(make_test_context: MakeContext, tmp_path: Path) -> None:
    def edit(doc: dict[str, Any]) -> None:
        p = project(doc, "pw-digital-gateway-va")
        p["operator"] = "Example Ventures (Jane Example) / 67 MW"
        p["owner"] = "Example Power Group (parcels) / SoftBank (Stargate)"
        p["tenant"] = "Amazon (AWS), Amazon"
        p["filing_llc"] = "Gateway Example LLC / Jane Example (developer)"

    result = run_edited(make_test_context, tmp_path, edit)
    pw = records(result)["pw-digital-gateway-va"]
    assert [o.name for o in pw.parties.developer] == ["Example Ventures"]
    assert [o.name for o in pw.parties.owner] == ["Example Power Group", "SoftBank"]
    assert [o.name for o in pw.parties.tenant] == ["Amazon"]
    assert [o.name for o in pw.parties.filing_entities] == ["Gateway Example LLC"]
    assert [a.name for a in pw.aliases] == ["Gateway Example LLC"]
    assert result.metrics["persons_dropped"] == 2
    (item,) = [i for i in result.review if i.kind == "unit_parse"]
    assert item.external_id == "pw-digital-gateway-va" and item.data == {"operator": "67 MW"}
    text = json.dumps([c.record.model_dump(mode="json") for c in result.candidates])
    assert "Jane" not in text and "67 MW" not in text


def test_a_nearby_place_gives_no_city_and_a_place_outside_the_county_gives_the_county(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    def edit(doc: dict[str, Any]) -> None:
        dated(doc)
        talen = project(doc, "talen-montour-pa")  # no coordinates
        talen.update({"locality": "Montour County (near Danville)"})
        stokes = project(doc, "stokes-county-project-delta")
        stokes.update({"locality": "Stokes County (Walnut Cove area)", "lat": "", "lon": ""})
        bristow = project(doc, "google-bristow")
        fairfax = counties.by_name("VA", "Fairfax County")
        assert fairfax is not None
        bristow["lat"], bristow["lon"] = counties.centroid(fairfax.fips)

    result = run_edited(make_test_context, tmp_path, edit)
    got = records(result)
    talen = got["talen-montour-pa"].location  # Danville is the seat of Montour County
    assert (talen.precision, talen.city, talen.county_name) == ("locality", None, "Montour")
    stokes = got["stokes-county-project-delta"].location
    assert (stokes.precision, stokes.city, stokes.county_name) == ("locality", None, "Stokes")
    assert got["stokes-county-project-delta"].canonical_name == "Project Delta (Stokes County, NC)"
    # AI GridWatch's point is outside the county its locality names: the text is taken over the
    # coordinates. Bristow is not a Census place, so the record is placed at Prince William
    # County's point, names that county, and a reviewer gets a county_mismatch item.
    bristow = got["google-bristow"].location
    assert (bristow.lat, bristow.lon) == counties.centroid(PRINCE_WILLIAM[0])
    assert (bristow.precision, bristow.geocode_method, bristow.city) == (
        "county",
        "county_centroid",
        "Bristow",
    )
    assert (bristow.county_fips, bristow.county_name) == PRINCE_WILLIAM
    (item,) = [i for i in result.review if i.kind == "county_mismatch"]
    assert item.external_id == "google-bristow" and "Prince William County" in item.reason
    assert "coordinates are not used" in item.reason


def test_a_nearby_place_outside_the_county_gives_the_county_centroid(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    def edit(doc: dict[str, Any]) -> None:
        dated(doc)
        # Danville lies in Montour County; Bloomsburg does not.
        project(doc, "talen-montour-pa")["locality"] = "Montour County (near Bloomsburg)"

    talen = records(run_edited(make_test_context, tmp_path, edit))["talen-montour-pa"].location
    assert (talen.precision, talen.geocode_method, talen.city) == (
        "county",
        "county_centroid",
        None,
    )
    assert talen.county_fips is not None
    assert (talen.lat, talen.lon) == counties.centroid(talen.county_fips)


# ---------------------------------------------------------------------------- Epoch AI's sites


def epoch_record(
    make_record: Callable[..., FacilityRecord], name: str, url: str, state: str = "VA"
) -> FacilityRecord:
    """A stored Epoch record: its dataset source plus one Selected Sources link."""
    rec = make_record(
        external_ids={"epoch_name": [name]},
        sources=[
            {
                "id": "s1",
                "url": "https://epoch.ai/data/ai-data-centers",
                "publisher": "Epoch AI",
                "source_type": "open_dataset",
                "license": "CC-BY-4.0",
                "retrieved_at": "2026-10-12T12:00:00Z",
                "supports": ["/canonical_name", "/location", "/capacity", "/parties"],
            },
            {
                "id": "s2",
                "url": url,
                "publisher": "example.org",
                "source_type": "news",
                "retrieved_at": "2026-10-12T12:00:00Z",
                "supports": [],
            },
        ],
    )
    if state != rec.location.state_abbr:
        rec = rec.model_copy(
            update={"location": rec.location.model_copy(update={"state_abbr": state})}
        )
    return rec


def test_an_epoch_site_is_not_imported_twice(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    doc = load_fixture()
    bristow_source = project(doc, "google-bristow")["source"]
    by_name = epoch_record(make_record, "Google Bristow", "https://example.org/other")
    by_source = epoch_record(
        make_record, "Google Bristow Campus", project(doc, "qts-richmond-3")["source"]
    )
    elsewhere = epoch_record(make_record, "Talen Energy Montour Data Center", bristow_source, "TX")
    stored = {r.id: r for r in (by_name, by_source, elsewhere)}

    def cite_epoch(d: dict[str, Any]) -> None:
        dated(d)
        project(d, "red-oak-compass-campus")["source"] = (
            "https://epoch.ai/publications/openai-stargate-where-the-us-sites-stand"
        )

    result = run_edited(make_test_context, tmp_path, cite_epoch, records=stored)
    got = records(result)
    dupes = {i.external_id: i for i in result.review if i.kind == "possible_duplicate"}
    assert set(dupes) == {"google-bristow", "qts-richmond-3", "red-oak-compass-campus"}
    assert not set(dupes) & set(got)
    assert (dupes["google-bristow"].record_id, dupes["google-bristow"].data["matched_by"]) == (
        by_name.id,
        "name",
    )
    assert (dupes["qts-richmond-3"].record_id, dupes["qts-richmond-3"].data["matched_by"]) == (
        by_source.id,
        "source",
    )
    # Both notes say "Epoch AI estimates ~… MW": AI GridWatch's copies of Epoch's sites.
    assert all(
        "republishes this Epoch AI site" in dupes[p].reason
        for p in ("google-bristow", "qts-richmond-3")
    )
    assert dupes["red-oak-compass-campus"].record_id is None
    assert dupes["red-oak-compass-campus"].data["matched_by"] == "epoch_source"
    # The same name in another state is another site.
    assert "talen-montour-pa" in got
    # A link that several Epoch records cite (a company report) is not specific to one site.
    shared = epoch_record(
        make_record, "Another Epoch Site", project(doc, "qts-richmond-3")["source"]
    )
    again = run_edited(
        make_test_context, tmp_path, cite_epoch, records={**stored, shared.id: shared}
    )
    assert "qts-richmond-3" in records(again)
    assert result.metrics["epoch_duplicates"] == 3 and result.metrics["epoch_records_seen"] == 3
    # Without Epoch records only the row that cites Epoch AI itself is held.
    alone = run_edited(make_test_context, tmp_path, cite_epoch)
    assert [i.external_id for i in alone.review if i.kind == "possible_duplicate"] == [
        "red-oak-compass-campus"
    ]


def test_the_source_rule_needs_the_same_state(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # An Epoch record in another state, under a name no AI GridWatch row carries, cites
    # qts-richmond-3's source: a link is the same site only within one state.
    doc = load_fixture()
    texas = epoch_record(
        make_record, "Example Texas Campus", project(doc, "qts-richmond-3")["source"], "TX"
    )
    result = run(make_test_context, records={texas.id: texas})
    assert "qts-richmond-3" in records(result)
    assert not [i for i in result.review if i.kind == "possible_duplicate"]


# ---------------------------------------------------------------------------- under other names

BRISTOW = (38.73, -77.54)  # google-bristow's point, in Prince William County (51153)
PRINCE_WILLIAM = ("51153", "Prince William")
HENRICO = ("51087", "Henrico")
STOKES = ("37169", "Stokes")


def epoch_site(
    make_record: Callable[..., FacilityRecord],
    name: str,
    *,
    links: tuple[str, ...] = (),
    owner: tuple[str, ...] = (),
    county: tuple[str, str] = PRINCE_WILLIAM,
    point: tuple[float, float] = BRISTOW,
    precision: str = "locality",
    street: str | None = None,
    state: str = "VA",
) -> FacilityRecord:
    """A stored Epoch record placed where the test needs it, citing `links`."""
    retrieved = "2026-10-12T12:00:00Z"
    return make_record(
        external_ids={"epoch_name": [name]},
        parties={"owner": [{"name": n, "source_ids": ["s1"]} for n in owner]},
        location={
            "lat": point[0],
            "lon": point[1],
            "precision": precision,
            "street": street,
            "county_fips": county[0],
            "county_name": county[1],
            "state_abbr": state,
            "geocode_method": "census_geocoder" if street else "gazetteer",
        },
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


def duplicates(result: ImportResult) -> dict[str, ReviewItem]:
    return {i.external_id or "": i for i in result.review if i.kind == "possible_duplicate"}


def stored(*recs: FacilityRecord) -> dict[str, FacilityRecord]:
    return {r.id: r for r in recs}


def test_an_id_that_is_an_epoch_name_is_the_same_site(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord]
) -> None:
    # AI GridWatch ids are names in slug form: "microsoft-nebius-new-jersey" is Epoch's
    # "Microsoft-Nebius New Jersey" under the name "DataOne USA / Nebius Vineland (Microsoft)",
    # and "qts-cedar-rapids-ia" is "QTS Cedar Rapids" with the state appended.
    qts = epoch_site(make_record, "QTS Richmond-3", county=HENRICO)
    talen = epoch_site(make_record, "Talen Montour", state="PA", county=("42093", "Montour"))
    other_state = epoch_site(make_record, "Meta LEAP Lebanon", state="OH")
    # Accents are dropped from the name: "Doña Ana Campus" is "dona-ana-campus" (NEW-4).
    accented = epoch_site(make_record, "Stókes Coünty Projéct Delta", state="NC", county=STOKES)
    result = run(make_test_context, records=stored(qts, talen, other_state, accented))
    dupes = duplicates(result)
    assert {k: (v.data["matched_by"], v.record_id) for k, v in dupes.items()} == {
        "qts-richmond-3": ("id", qts.id),
        "talen-montour-pa": ("id", talen.id),
        "stokes-county-project-delta": ("id", accented.id),
    }
    assert "meta-leap-lebanon-in" in records(result)  # the same slug in another state
    # Talen's note quotes no Epoch AI estimate: it is AI GridWatch's own row, whose milestones are
    # not merged into the Epoch record (n42); QTS's copy says "Epoch AI estimates".
    assert "own row for this Epoch site" in dupes["talen-montour-pa"].reason
    assert "republishes" not in dupes["talen-montour-pa"].reason
    assert "republishes this Epoch AI site" in dupes["qts-richmond-3"].reason


def test_a_link_only_in_the_event_log_holds_the_row_without_merging(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # OpenAI Stargate New Mexico: AI GridWatch's own row (another name) cites, in its event log,
    # the Oracle release that Epoch cites for the site. An event log is not the row's own claim
    # (AI GridWatch copied New Carlisle's history into its row on Amazon's Northern Indiana
    # expansion), so the row is held for review with the site as a candidate, not merged.
    doc = load_fixture()
    link = project(doc, "google-bristow")["events"][0]["source"]
    site = epoch_site(make_record, "Example Epoch Campus", links=(link,))
    result = run(make_test_context, records=stored(site))
    item = duplicates(result)["google-bristow"]
    assert (item.data["matched_by"], item.record_id, item.data["evidence"]) == (
        "weak_link",
        None,
        link,
    )
    assert item.data["epoch_records"] == [site.id]
    assert "only in the row's event log" in item.reason and "not merged" in item.reason
    assert "google-bristow" not in records(result)
    assert result.metrics["epoch_duplicates"] == 1 and result.metrics["epoch_ambiguous"] == 1
    # The same link from an Epoch site in another county ties nothing.
    henrico = epoch_site(make_record, "Example Epoch Campus", links=(link,), county=HENRICO)
    assert "google-bristow" in records(run(make_test_context, records=stored(henrico)))

    # A link another AI GridWatch row also cites is not specific to one site.
    def shared(d: dict[str, Any]) -> None:
        project(d, "pw-digital-gateway-va")["events"].append({"date": "", "source": link})

    again = run_edited(make_test_context, tmp_path, shared, records=stored(site))
    assert "google-bristow" in records(again) and not duplicates(again)


def test_a_link_shared_with_ai_gridwatchs_copy_of_an_epoch_site_is_the_same_site(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # AI GridWatch's copy of the Epoch site (held by name) and its own row under another name
    # cite the same local news story; the row's own source is that story, so it is the same site.
    story = "https://example.org/news/groundbreaking-at-the-campus"
    site = epoch_site(make_record, "Example Epoch Campus")

    def add_copy(
        d: dict[str, Any], *, cited_elsewhere: bool = False, own_source: bool = True
    ) -> None:
        copy_row = copy.deepcopy(project(d, "google-bristow"))
        copy_row.update(
            {
                "id": "example-epoch-campus",
                "name": "Example Epoch Campus",
                "source": "https://example.org/epoch-first-source",
                "events": [{"date": "2026-01-05", "kind": "construction", "source": story}],
            }
        )
        d["projects"].append(copy_row)
        bristow = project(d, "google-bristow")
        if own_source:
            bristow["source"] = story
        else:
            bristow["events"].append({"date": "2026-01-05", "source": story})
        if cited_elsewhere:
            project(d, "pw-digital-gateway-va")["events"].append({"date": "", "source": story})

    result = run_edited(make_test_context, tmp_path, add_copy, records=stored(site))
    dupes = duplicates(result)
    assert dupes["example-epoch-campus"].data["matched_by"] == "name"
    item = dupes["google-bristow"]
    assert (item.data["matched_by"], item.record_id, item.data["evidence"]) == (
        "link",
        site.id,
        story,
    )
    # The row's milestones are not merged into the Epoch record: the reason says so.
    assert "own row for this Epoch site" in item.reason and "M4" in item.reason
    # The story only in the row's event log: held for review, not merged.
    logged = run_edited(
        make_test_context,
        tmp_path,
        lambda d: add_copy(d, own_source=False),
        records=stored(site),
    )
    assert duplicates(logged)["google-bristow"].data["matched_by"] == "weak_link"
    # The copy's site in another county (Google's Texas announcement covers sites 260 km
    # apart), or a third row citing the story: no tie.
    henrico = epoch_site(make_record, "Example Epoch Campus", county=HENRICO)
    far = run_edited(make_test_context, tmp_path, add_copy, records=stored(henrico))
    assert "google-bristow" in records(far)
    crowded = run_edited(
        make_test_context,
        tmp_path,
        lambda d: add_copy(d, cited_elsewhere=True),
        records=stored(site),
    )
    assert "google-bristow" in records(crowded)


def test_a_link_with_mw_more_than_twice_apart_holds_the_row(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # aws-new-carlisle-in (2,400 MW, Amazon's expansion to new Indiana sites) against Epoch's
    # Anthropic-Amazon New Carlisle (910 MW IT): more than x2 apart, so even a link of its own does
    # not make it the same site (07 §6.6, Size). Within x2, it does.
    story = "https://example.org/news/the-campus"

    def add_copy(size_mw: int) -> Callable[[dict[str, Any]], None]:
        def edit(d: dict[str, Any]) -> None:
            copy_row = copy.deepcopy(project(d, "google-bristow"))
            copy_row.update({"id": "example-epoch-campus", "name": "Example Epoch Campus"})
            copy_row.update({"source": story, "events": []})
            d["projects"].append(copy_row)
            project(d, "google-bristow").update({"source": story, "size_mw": size_mw})

        return edit

    site = epoch_site(make_record, "Example Epoch Campus")
    site = site.model_copy(update={"capacity": site.capacity.model_copy(update={"it_mw": 910.0})})
    far = duplicates(run_edited(make_test_context, tmp_path, add_copy(2400), records=stored(site)))
    item = far["google-bristow"]
    assert (item.data["matched_by"], item.record_id) == ("weak_link", None)
    assert item.data["epoch_records"] == [site.id]
    assert "2400 MW and the site's 910 MW differ by more than x2" in item.reason
    near = duplicates(run_edited(make_test_context, tmp_path, add_copy(1500), records=stored(site)))
    assert (near["google-bristow"].data["matched_by"], near["google-bristow"].record_id) == (
        "link",
        site.id,
    )


def test_a_name_that_gives_the_epoch_sites_street_address_is_the_same_site(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # CoreWeave Lancaster Greenfield site (216 Greenfield Rd) is AI GridWatch's "Lancaster AI Hub
    # East [formerly referred to as 216 Greenfield Road (Chirisa Technology Parks)]".
    bulloch = ("13031", "Bulloch")
    site = epoch_site(
        make_record,
        "Example Statesboro Campus",
        county=bulloch,
        point=(32.45, -81.78),
        precision="address",
        street="216 Greenfield Rd",
        state="GA",
    )

    def rename(d: dict[str, Any]) -> None:
        project(d, "burkhalter-road-statesboro-ga")["name"] = (
            "Statesboro AI Hub [formerly referred to as 216 Greenfield Road]"
        )

    result = run_edited(make_test_context, tmp_path, rename, records=stored(site))
    item = duplicates(result)["burkhalter-road-statesboro-ga"]
    assert (item.data["matched_by"], item.record_id) == ("street", site.id)
    # Another house number, or the same street address in another county, is another site.
    other = site.model_copy(
        update={"location": site.location.model_copy(update={"street": "218 Greenfield Rd"})}
    )
    assert "burkhalter-road-statesboro-ga" in records(
        run_edited(make_test_context, tmp_path, rename, records=stored(other))
    )
    elsewhere = site.model_copy(
        update={
            "location": site.location.model_copy(
                update={"county_fips": "13051", "county_name": "Chatham"}
            )
        }
    )
    assert "burkhalter-road-statesboro-ga" in records(
        run_edited(make_test_context, tmp_path, rename, records=stored(elsewhere))
    )


def test_an_organization_in_common_nearby_is_held_for_review_not_merged(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord]
) -> None:
    # AWS Berwick (Epoch, at Berwick's point) and AI GridWatch's "AWS Salem Township campus" 4.3 km
    # away share only Amazon: the row is held, and no Epoch record is named as the same site.
    near = epoch_site(
        make_record, "Example Google Campus", owner=("Google",), point=(38.748, -77.54)
    )
    result = run(make_test_context, records=stored(near))
    item = duplicates(result)["google-bristow"]
    assert (item.data["matched_by"], item.record_id, item.data["epoch_records"]) == (
        "nearby",
        None,
        [near.id],
    )
    assert f"Example Google Campus ({near.id})" in item.reason and "not merged" in item.reason
    assert "google-bristow" not in records(result) and result.metrics["epoch_ambiguous"] == 1
    # Farther than 5 km, another organization, or a county-level point: no tie.
    for site in (
        epoch_site(make_record, "Far", owner=("Google",), point=(38.80, -77.54)),
        epoch_site(make_record, "Other", owner=("Meta",), point=(38.748, -77.54)),
        epoch_site(make_record, "Coarse", owner=("Google",), precision="county"),
    ):
        assert "google-bristow" in records(run(make_test_context, records=stored(site))), site


def test_a_row_at_its_county_point_is_not_nearby_anything(
    make_test_context: MakeContext,
    make_record: Callable[..., FacilityRecord],
    tmp_path: Path,
    counties: CountyIndex,
) -> None:
    # The nearby rule needs both points at locality precision or finer (NEW-4: the Epoch side's
    # condition had a test, the AI GridWatch side's did not). Talen has no coordinates and is
    # placed at Montour County's point: a precise Epoch site of Talen's 1 km from that point is
    # not "nearby", since the row's point says nothing about where the site is.
    montour = ("42093", "Montour")
    lat, lon = counties.centroid(montour[0])
    site = epoch_site(
        make_record,
        "Example Talen Campus",
        owner=("Talen Energy",),
        state="PA",
        county=montour,
        point=(lat + 0.009, lon),
    )

    def dated_owned(doc: dict[str, Any]) -> None:
        dated(doc)
        project(doc, "talen-montour-pa")["owner"] = "Talen Energy"

    result = run_edited(make_test_context, tmp_path, dated_owned, records=stored(site))
    talen = records(result)["talen-montour-pa"]
    assert (talen.location.precision, talen.location.geocode_method) == (
        "county",
        "county_centroid",
    )
    assert "talen-montour-pa" not in duplicates(result)


def test_organizations_are_compared_without_an_alias_table(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # xai-colossus-memphis-tn names "xAI"; Epoch's Colossus 1 and Colossus 2 name "SpaceXAI". A
    # link ties the row to Colossus 2 alone, but Colossus 1, in the same county, may share its
    # organization: the row is held as `several`, naming both, not merged into Colossus 2 (n37).
    assert org_key("X.AI Corp") == org_key("xAI") == "xai" and org_key("SpaceXAI") == "spacexai"
    assert orgs_overlap(frozenset({"xai"}), frozenset({"spacexai"}))
    assert not orgs_overlap(frozenset({"ai"}), frozenset({"openai"}))  # too short to tell
    doc = load_fixture()
    link = project(doc, "google-bristow")["events"][0]["source"]
    two = epoch_site(make_record, "Example Colossus 2", links=(link,), owner=("SpaceXAI",))
    one = epoch_site(make_record, "Example Colossus 1", owner=("SpaceXAI",), point=(38.75, -77.5))

    def xai(d: dict[str, Any]) -> None:
        project(d, "google-bristow").update({"operator": "xAI", "tenant": "xAI"})

    result = run_edited(make_test_context, tmp_path, xai, records=stored(one, two))
    item = duplicates(result)["google-bristow"]
    assert (item.data["matched_by"], item.record_id) == ("several", None)
    assert item.data["epoch_records"] == sorted([one.id, two.id])


def test_a_row_citing_an_epoch_ai_page_lists_the_sites_it_may_be(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # stargate-abilene-tx cites Epoch's Stargate report: it is held (epoch_source) without a
    # record id, and the reviewer now sees the Epoch site at the same point with an organization
    # in common (n39). Nothing is merged.
    site = epoch_site(
        make_record, "Example Google Campus", owner=("Google",), point=(38.731, -77.54)
    )

    def cite_epoch(d: dict[str, Any]) -> None:
        project(d, "google-bristow")["source"] = (
            "https://epoch.ai/publications/openai-stargate-where-the-us-sites-stand"
        )

    result = run_edited(make_test_context, tmp_path, cite_epoch, records=stored(site))
    item = duplicates(result)["google-bristow"]
    assert (item.data["matched_by"], item.record_id) == ("epoch_source", None)
    assert item.data["epoch_records"] == [site.id] and f"({site.id})" in item.reason
    assert "google-bristow" not in records(result)


def test_a_reviewer_releases_a_held_row(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # NEW-2: a row the nearby rule holds, which a reviewer finds is another site, is imported
    # through config/overrides/aigridwatch.json, and an item notes the release.
    near = epoch_site(
        make_record, "Example Google Campus", owner=("Google",), point=(38.748, -77.54)
    )
    held = run(make_test_context, records=stored(near))
    assert "google-bristow" not in records(held)
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "google-bristow": {
                    "release": ["epoch"],
                    "reason": "test: the county's site plan shows two campuses",
                    "reviewed_at": "2026-10-09",
                }
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(without_epoch=False, overrides=overrides)
    result = IMPORTER.run(make_test_context(input_path=FIXTURE, records=stored(near)), args)
    assert "google-bristow" in records(result)
    (item,) = duplicates(result).values()
    assert item.external_id == "google-bristow" and item.record_id is None
    assert (item.data["matched_by"], item.data["released"], item.data["epoch_records"]) == (
        "nearby",
        "epoch",
        [near.id],
    )
    assert "released by config/overrides/aigridwatch.json" in item.reason
    assert result.metrics["released"] == 1 and result.metrics["overrides_used"] == 1
    # A row held by its name, released: one item notes it, and the location rules do not run.
    named = epoch_site(make_record, "Google Bristow", owner=("Google",))
    by_name = IMPORTER.run(
        make_test_context(input_path=FIXTURE, records=stored(named)),
        argparse.Namespace(without_epoch=False, overrides=overrides),
    )
    assert "google-bristow" in records(by_name)
    assert [i.data["matched_by"] for i in duplicates(by_name).values()] == ["name"]
    # With no overrides file at the default path, nothing is released.
    assert (
        IMPORTER.run(
            make_test_context(input_path=FIXTURE, records=stored(near)),
            argparse.Namespace(without_epoch=False, overrides=OVERRIDES_PATH),
        ).metrics["overrides_used"]
        == 0
    )


def test_a_row_tied_to_more_than_one_epoch_site_is_held_for_review_not_merged(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # AWS Madison County data center campuses: tied by a link to Amazon Madison Mega Site, and
    # Amazon also owns Epoch's Amazon Ridgeland in the same county.
    doc = load_fixture()
    first, second = (e["source"] for e in project(doc, "google-bristow")["events"])
    by_link = epoch_site(make_record, "Example Campus One", links=(first,))
    by_owner = epoch_site(make_record, "Example Campus Two", owner=("Google",), point=(38.8, -77.5))
    by_other_link = epoch_site(make_record, "Example Campus Three", links=(second,))
    for pair in ((by_link, by_owner), (by_link, by_other_link)):
        result = run(make_test_context, records=stored(*pair))
        item = duplicates(result)["google-bristow"]
        assert (item.data["matched_by"], item.record_id) == ("several", None)
        assert item.data["epoch_records"] == sorted(r.id for r in pair)
        assert "google-bristow" not in records(result)

    # A held row's own review items go with it: it is not imported.
    def bad_size(d: dict[str, Any]) -> None:
        project(d, "google-bristow")["size_mw"] = "lots"

    result = run_edited(make_test_context, tmp_path, bad_size, records=stored(by_link))
    assert duplicates(result)["google-bristow"].data["matched_by"] == "weak_link"
    assert not [i for i in result.review if i.kind == "unit_parse"]


def test_a_twin_whose_stage_disagrees_with_the_epoch_record_is_flagged(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord]
) -> None:
    """n36: Google Fort Wayne is Operating in AI GridWatch and under construction in Epoch's count
    of AI buildings. The row is held as the Epoch record's twin, so its stage is not applied; a
    `conflict` item says they disagree, for a reviewer to check (the status is not changed)."""
    from atlas.schema.rollup import apply_rollup

    def at(rec: FacilityRecord, status: str, event: str) -> FacilityRecord:
        first = rec.status_history[0].model_copy(update={"status": status, "event": event})
        return apply_rollup(rec.model_copy(update={"status_history": [first]}))

    bristow = at(
        epoch_site(make_record, "Google Bristow"), "under_construction", "construction_start"
    )
    red_oak = at(
        epoch_site(
            make_record,
            "Compass Datacenters Red Oak campus",
            state="TX",
            county=("48139", "Ellis"),
            point=(32.52, -96.8),
        ),
        "permitted",
        "approved",
    )
    pw = at(
        epoch_site(make_record, "PW Digital Gateway", point=(38.72, -77.55)), "proposed", "other"
    )
    result = run(make_test_context, records=stored(bristow, red_oak, pw))
    assert {"google-bristow", "red-oak-compass-campus", "pw-digital-gateway-va"} <= set(
        duplicates(result)
    )
    conflicts = {
        i.external_id: i for i in result.review if i.kind == "conflict" and "epoch_status" in i.data
    }
    assert set(conflicts) == {"google-bristow", "pw-digital-gateway-va"}  # Red Oak agrees
    item = conflicts["google-bristow"]
    assert item.record_id == bristow.id
    assert item.data == {
        "stage": "Operating",
        "as_of": "2026-08-14",
        "epoch_status": "under_construction",
        "support": None,
    }
    assert "is not applied" in item.reason and "disagrees" in item.reason
    # Withdrawn on 2026-04-01: the decision supports the stage.
    assert conflicts["pw-digital-gateway-va"].data["support"] == {
        "event": "withdrawn",
        "as_of": "2026-04",
    }
    assert result.metrics["epoch_stage_conflicts"] == 2
    # Nothing is imported for the held rows, and the Epoch records are not changed.
    assert {"google-bristow", "pw-digital-gateway-va"}.isdisjoint(records(result))


def test_a_new_england_town_is_placed_as_a_county_subdivision(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    """n40: Bloomfield, CT is a town; the Gazetteer's only place there is Blue Hills CDP. With
    the county subdivisions (on for every import that geocodes) the row is placed at the town's
    point, in the Capitol Planning Region, with Bloomfield as its municipality."""
    from atlas.geocode import Gazetteer

    def bloomfield(doc: dict[str, Any]) -> None:
        row = project(doc, "talen-montour-pa")
        row["state"], row["locality"], row["name"] = "CT", "Bloomfield", "Example Bloomfield campus"

    result = run_edited(make_test_context, tmp_path, bloomfield)
    rec = records(result)["talen-montour-pa"]
    loc = rec.location
    assert (loc.precision, loc.geocode_method, loc.county_fips) == (
        "locality",
        "gazetteer",
        "09110",
    )
    assert (loc.lat, loc.lon) == (41.843772, -72.741002)
    assert (loc.municipality, loc.city) == ("Bloomfield", None)
    assert rec.canonical_name == "Example Bloomfield campus (Bloomfield, CT)"
    # Without the county subdivisions the town cannot be placed.
    without = run_edited(make_test_context, tmp_path, bloomfield, gazetteer=Gazetteer.load())
    assert "talen-montour-pa" not in records(without)
    assert ("geocode_failed", "talen-montour-pa") in {
        (i.kind, i.external_id) for i in without.review
    }


def test_the_rows_source_type_reads_its_path(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    """classify_source: an ISO's filing is not news, and a permit portal is a government record."""

    def sources(doc: dict[str, Any]) -> None:
        project(doc, "qts-richmond-3")["source"] = "https://cdn.misoenergy.org/new-load.pdf"
        project(doc, "google-bristow")["source"] = "https://example.civicweb.net/document/1"

    got = records(run_edited(make_test_context, tmp_path, sources))
    assert got["qts-richmond-3"].sources[1].source_type == "utility_or_iso_filing"
    assert got["google-bristow"].sources[1].source_type == "government_record"
    assert got["stokes-county-project-delta"].sources[1].source_type == "news"


def test_the_run_needs_the_epoch_records(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord]
) -> None:
    # aigridwatch before epoch on an empty store would import every Epoch site AI GridWatch
    # republishes (69 on 2026-10-08), and those records would stay after the next run.
    with pytest.raises(FetchError, match="run `atlas import epoch` first"):
        run(make_test_context, without_epoch=False)
    site = epoch_site(make_record, "Google Bristow")
    merged = site.model_copy(update={"merged_into": "gwa-01m47854008j5vt37xkj5ag72d"})
    with pytest.raises(FetchError, match="--without-epoch"):
        run(make_test_context, without_epoch=False, records=stored(merged))
    result = run(make_test_context, without_epoch=False, records=stored(site))
    assert duplicates(result)["google-bristow"].record_id == site.id


def test_a_stored_record_for_a_row_now_held_is_named(
    make_test_context: MakeContext,
    make_record: Callable[..., FacilityRecord],
    tmp_repo: Path,
) -> None:
    # An AI GridWatch record stored before its Epoch twin arrived is kept (records are never
    # deleted); the duplicate item names it next to the Epoch record.
    site = epoch_site(make_record, "Google Bristow")
    old = make_record(external_ids={"aigridwatch_id": ["google-bristow"]})
    store = RecordStore(tmp_repo / "data" / "records")
    for r in (site, old):
        store.write(r)
    ctx = make_test_context(input_path=FIXTURE, records=store.load())
    result = IMPORTER.run(ctx, argparse.Namespace(without_epoch=False))
    item = duplicates(result)["google-bristow"]
    assert (item.record_id, item.data["stored_record"]) == (site.id, old.id)
    assert old.id in item.reason and "merged_into" in item.reason
    receipt = apply_import(
        IMPORTER,
        result,
        ctx,
        store=store,
        review_dir=tmp_repo / "review" / "queue",
        receipts_dir=tmp_repo / "data" / "imports",
    )
    assert receipt.counts["removed_upstream"] == 1
    queue = read_review_queue(tmp_repo / "review" / "queue", "aigridwatch")
    assert {
        (i.kind, i.record_id) for i in queue if old.id in (i.record_id, i.data.get("stored_record"))
    } == {
        ("removed_upstream", old.id),
        ("possible_duplicate", site.id),
    }
    # Once a reviewer has merged it, there is nothing left to point out.
    merged = old.model_copy(update={"merged_into": site.id})
    again = run(make_test_context, without_epoch=False, records=stored(site, merged))
    assert "stored_record" not in duplicates(again)["google-bristow"].data


# ---------------------------------------------------------------------------- guards and the CLI


def test_license_and_layout_guards(make_test_context: MakeContext, tmp_path: Path) -> None:
    def relicense(doc: dict[str, Any]) -> None:
        doc["license"] = "All rights reserved"

    with pytest.raises(FetchError, match="license"):
        run_edited(make_test_context, tmp_path, relicense)

    def no_projects(doc: dict[str, Any]) -> None:
        del doc["projects"]

    with pytest.raises(FetchError, match="no projects list"):
        run_edited(make_test_context, tmp_path, no_projects)
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(FetchError, match="not JSON"):
        run(make_test_context, bad)


def test_cli_run_is_idempotent(
    tmp_repo: Path,
    repo_root: Path,
    capsys: pytest.CaptureFixture[str],
    census_reference_cache: Callable[[Path], Path],
) -> None:
    census_reference_cache(tmp_repo / ".cache")  # the county-subdivision sample, as if downloaded
    common = [
        "import",
        "aigridwatch",
        "--input",
        str(FIXTURE),
        "--records",
        str(tmp_repo / "data" / "records"),
        "--review-dir",
        str(tmp_repo / "review" / "queue"),
        "--receipts-dir",
        str(tmp_repo / "data" / "imports"),
        "--cache-dir",
        str(tmp_repo / ".cache"),
        "--counties",
        str(repo_root / "reference" / "census" / "cb_2025_us_county_500k.zip"),
        "--now",
        "2026-10-12T12:00:00Z",
        "--offline",
    ]
    # The store holds no Epoch record, so the run refuses until told it may go without them.
    assert main(common) == 1
    assert "run `atlas import epoch` first" in capsys.readouterr().err
    assert not (tmp_repo / "data" / "imports" / "aigridwatch.json").exists()
    common.append("--without-epoch")
    assert main(common) == 0
    assert "import aigridwatch: candidates=11 new=11" in capsys.readouterr().out
    receipt = json.loads((tmp_repo / "data" / "imports" / "aigridwatch.json").read_text("utf-8"))
    # 4: milestones read as the row explains them, places the row puts the site outside, the
    # holds of the fifth fix round
    assert receipt["importer_version"] == "4"
    stored = RecordStore(tmp_repo / "data" / "records").load()
    assert len(stored) == 11  # every verified row
    queue = (tmp_repo / "review" / "queue" / "aigridwatch.jsonl").read_text(encoding="utf-8")
    assert '"kind":"unverified_upstream"' in queue
    before = snapshot(tmp_repo / "data" / "records", tmp_repo / "review")
    assert main(common) == 0
    assert "unchanged=11" in capsys.readouterr().out
    assert snapshot(tmp_repo / "data" / "records", tmp_repo / "review") == before
    everything = snapshot(tmp_repo / "data", tmp_repo / "review")
    assert main(common) == 0
    capsys.readouterr()
    assert snapshot(tmp_repo / "data", tmp_repo / "review") == everything


def test_fetch_uses_the_published_url(
    make_test_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("atlas.net._Fetcher._wait_for_host", lambda self, url: None)
    body = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROJECTS_URL:
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/json; charset=utf-8", "etag": '"e1"'},
                request=request,
            )
        return httpx.Response(404, request=request)

    ctx = make_test_context(handler=handler)
    result = IMPORTER.run(ctx, argparse.Namespace(without_epoch=True))
    (snap,) = result.inputs
    assert snap.upstream_version == "2026-10-07" and snap.etag == '"e1"'
    assert ctx.raw_path("aigridwatch", snap.sha256, "json").read_bytes() == body
    assert all(isinstance(c, Candidate) for c in result.candidates)


@pytest.mark.network
def test_live_aigridwatch_json() -> None:
    with make_client() as client:
        got = fetch(client, PROJECTS_URL, allowed_types=("application/json",))
    doc = json.loads(got.content)
    assert doc["license"] == "CC BY 4.0"
    assert len(doc["projects"]) >= 200
