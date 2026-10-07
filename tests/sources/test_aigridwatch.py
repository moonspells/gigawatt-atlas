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
    filing_names,
    has_filing,
    looks_like_person,
    milestone_events,
    parse_locality,
    resolve_county,
    row_source_type,
)
from atlas.sources.base import Candidate, ImportContext, ImportResult
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


def run(make_test_context: MakeContext, path: Path = FIXTURE, **ctx: Any) -> ImportResult:
    return IMPORTER.run(make_test_context(input_path=path, **ctx), argparse.Namespace())


def run_edited(
    make_test_context: MakeContext,
    tmp_path: Path,
    edit: Callable[[dict[str, Any]], None],
    **ctx: Any,
) -> ImportResult:
    doc = copy.deepcopy(load_fixture())
    edit(doc)
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return run(make_test_context, path, **ctx)


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


def test_party_helpers() -> None:
    assert looks_like_person("Jane Example (developer)")
    assert not looks_like_person("Example Holdings LLC (developer)")
    assert not looks_like_person("Constellation Energy Group (parcels)")
    assert not looks_like_person("Meta")
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
    assert pw.capacity.it_mw is None and len(pw.sources) == 1
    assert ("unit_parse", "pw-digital-gateway-va") in {
        (i.kind, i.external_id) for i in result.review
    }


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
        str(repo_root / "reference" / "census" / "cb_2025_us_county_5m.zip"),
        "--now",
        "2026-10-12T12:00:00Z",
        "--offline",
    ]
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
    result = IMPORTER.run(ctx, argparse.Namespace())
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
