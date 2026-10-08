"""The AI GridWatch importer (atlas/sources/aigridwatch.py) against the committed fixture."""

from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas.cli import main
from atlas.crosswalk import from_aigridwatch_stage
from atlas.geo.counties import CountyIndex
from atlas.net import FetchError, fetch, make_client
from atlas.schema.record import FacilityRecord
from atlas.sources.aigridwatch import (
    IMPORTER,
    PROJECTS_URL,
    PartyName,
    filing_names,
    has_filing,
    late_announcement,
    looks_like_person,
    looks_like_person_name,
    milestone_events,
    parse_locality,
    party_name,
    resolve_county,
    row_source_type,
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
    "pw-digital-gateway-va": ("Withdrawn", "cancelled", "developer_withdrawal", "reported"),
    "talen-montour-pa": ("Denied", "denied", "local_denial", "reported"),
    "meta-leap-lebanon-in": ("Proposed", "announced", None, "reported"),
    "abei-energy-data-center-starke-in": ("Blocked by ban", "paused", "moratorium", "reported"),
}


def load_fixture() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return doc


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
    assert row_source_type("https://gis.dep.pa.gov/DataCenterPermitTracker/") == "government_record"
    assert row_source_type("https://example.legistar.com/x") == "government_record"
    assert row_source_type("https://www.wfdd.org/x") == "news"


# ---------------------------------------------------------------------------- stages and events


def test_every_stage_maps_through_the_crosswalk(
    make_test_context: MakeContext, counties: CountyIndex, today: date
) -> None:
    result = run(make_test_context)
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


def test_proposed_splits_on_a_filing(make_test_context: MakeContext, tmp_path: Path) -> None:
    pid = "meta-leap-lebanon-in"

    def add_filing(doc: dict[str, Any]) -> None:
        project(doc, pid)["rezoning_filed"] = "2026-03-01"

    rec = records(run_edited(make_test_context, tmp_path, add_filing))[pid]
    assert rec.status == "proposed"
    assert [(e.event, e.as_of.value) for e in rec.status_history] == [
        ("announced", "2026-02-11"),
        ("application_filed", "2026-03-01"),
    ]

    def filing_event(doc: dict[str, Any]) -> None:
        project(doc, pid)["events"].append(
            {"date": "2026-03-01", "kind": "filing", "summary": "x", "source": ""}
        )

    rec = records(run_edited(make_test_context, tmp_path, filing_event))[pid]
    assert rec.status == "proposed"
    assert rec.status_history[-1].event == "other"  # the stage's status, with no dated filing


def test_milestones_that_agree_with_the_stage_add_no_event(make_test_context: MakeContext) -> None:
    got = records(run(make_test_context))
    red_oak = got["red-oak-compass-campus"]
    assert [
        (e.seq, e.event, e.status, e.as_of.value, e.planned) for e in red_oak.status_history
    ] == [
        (1, "hearing_held", "proposed", "2026-05-11", False),
        (2, "approved", "permitted", "2026-05-12", False),
    ]
    assert red_oak.dates["approved"].value == "2026-05-12"
    pw = got["pw-digital-gateway-va"]
    assert [(e.event, e.status) for e in pw.status_history] == [("withdrawn", "cancelled")]
    assert pw.dates["cancelled"].value == "2026-04-01"
    assert all(e.source_ids == ["s1"] and e.as_of.precision == "day" for e in pw.status_history)


def test_a_stage_the_milestones_do_not_reach_adds_an_other_event(
    make_test_context: MakeContext,
) -> None:
    abei = records(run(make_test_context))["abei-energy-data-center-starke-in"]
    *milestones, other = abei.status_history
    assert [e.event for e in milestones] == ["announced", "application_filed", "hearing_held"]
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
    # Not proposed (filed) -> announced -> proposed: the late announcement is dropped.
    assert [(e.event, e.status, e.as_of.value) for e in rec.status_history] == [
        ("application_filed", "proposed", "2026-01-15"),
    ]
    assert "announced" not in rec.dates and rec.dates["first_reported"].value == "2026-01-15"
    (item,) = [i for i in result.review if i.kind == "conflict"]
    assert item.external_id == pid
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
    after = milestone_events(red_oak, date(2026, 10, 12))
    assert [(e["event"], e["planned"]) for e in after] == [
        ("hearing_held", False),
        ("approved", False),
    ]
    # Run as of 2026-05-01: the planned events do not set the status; the stage does.
    early = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    rec = records(run(make_test_context, now=early, today=early.date()))["red-oak-compass-campus"]
    assert [(e.event, e.planned) for e in rec.status_history] == [
        ("hearing_scheduled", True),
        ("approved", True),
        ("other", False),
    ]
    assert rec.status == "permitted"


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
    assert len(result.candidates) == 8


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
    assert stokes.canonical_name == "Project Delta (Walnut Cove, NC)"
    assert stokes.location.city == "Walnut Cove" and stokes.location.county_name == "Stokes"
    pw = got["pw-digital-gateway-va"]
    assert pw.canonical_name == "PW Digital Gateway (Prince William County, VA)"
    assert pw.location.city is None and pw.location.county_name == "Prince William"


def test_rows_without_coordinates_are_geocoded(
    make_test_context: MakeContext, counties: CountyIndex
) -> None:
    got = records(run(make_test_context))
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
    assert (loc.precision, loc.geocode_method, loc.city, loc.municipality) == (
        "locality",
        "gazetteer",
        "Indianapolis",  # from the "(Indianapolis)" hint, since a township is not a Census place
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
    assert [o.name for o in got["pw-digital-gateway-va"].parties.operator] == [
        "Compass Datacenters",
        "QTS",
    ]
    bristow = got["google-bristow"]
    assert [o.name for o in bristow.parties.tenant] == ["Google DeepMind"]
    assert (
        bristow.capacity.it_mw == 279.0
        and bristow.field_meta["/capacity/it_mw"].method == "imported"
    )
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
    assert [o.name for o in stokes.parties.operator] == ["Engineered Land Solutions"]  # "(ELS)"
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
    assert [o.name for o in pw.parties.operator] == ["Example Ventures"]
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
    # AI GridWatch's point is outside the county its locality names: the record keeps the point,
    # names no county, and a reviewer gets a county_mismatch item.
    bristow = got["google-bristow"].location
    assert (bristow.city, bristow.county_fips, bristow.county_name) == ("Bristow", None, None)
    (item,) = [i for i in result.review if i.kind == "county_mismatch"]
    assert item.external_id == "google-bristow" and "Prince William County" in item.reason


def test_a_nearby_place_outside_the_county_gives_the_county_centroid(
    make_test_context: MakeContext, tmp_path: Path, counties: CountyIndex
) -> None:
    def edit(doc: dict[str, Any]) -> None:
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
    result = run(make_test_context, records=stored(qts, talen, other_state))
    dupes = duplicates(result)
    assert {k: (v.data["matched_by"], v.record_id) for k, v in dupes.items()} == {
        "qts-richmond-3": ("id", qts.id),
        "talen-montour-pa": ("id", talen.id),
    }
    assert "meta-leap-lebanon-in" in records(result)  # the same slug in another state


def test_a_link_specific_to_an_epoch_site_in_its_county_is_the_same_site(
    make_test_context: MakeContext, make_record: Callable[..., FacilityRecord], tmp_path: Path
) -> None:
    # OpenAI Stargate New Mexico: AI GridWatch's own row (another name) cites, in its event log,
    # the Oracle release that Epoch cites for the site.
    doc = load_fixture()
    link = project(doc, "google-bristow")["events"][0]["source"]
    site = epoch_site(make_record, "Example Epoch Campus", links=(link,))
    result = run(make_test_context, records=stored(site))
    item = duplicates(result)["google-bristow"]
    assert (item.data["matched_by"], item.record_id, item.data["evidence"]) == (
        "link",
        site.id,
        link,
    )
    assert "google-bristow" not in records(result)
    assert result.metrics["epoch_duplicates"] == 1 and result.metrics["epoch_ambiguous"] == 0
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
    # AWS New Carlisle: AI GridWatch's copy of the Epoch site (held by name) and its own row
    # under another name cite the same local news stories.
    story = "https://example.org/news/groundbreaking-at-the-campus"
    site = epoch_site(make_record, "Example Epoch Campus")

    def add_copy(d: dict[str, Any], *, cited_elsewhere: bool = False) -> None:
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
        project(d, "google-bristow")["events"].append({"date": "2026-01-05", "source": story})
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
    assert duplicates(result)["google-bristow"].data["matched_by"] == "link"
    assert not [i for i in result.review if i.kind == "unit_parse"]


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
    tmp_repo: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    stored = RecordStore(tmp_repo / "data" / "records").load()
    assert len(stored) == 11
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
