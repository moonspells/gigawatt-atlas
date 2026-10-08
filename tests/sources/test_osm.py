from __future__ import annotations

import argparse
import json
import urllib.parse
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import HttpUrl

from atlas.cli import main
from atlas.commands.importer import discover_importers
from atlas.dissolve import Bounds, OsmObject, dissolve, meters_to_degrees
from atlas.geo.counties import CountyIndex
from atlas.jsonio import record_json
from atlas.net import FetchError, make_client
from atlas.schema.record import PLACEHOLDER_ID, FacilityRecord, FuzzyDate
from atlas.schema.rollup import is_expanding, phase_statuses
from atlas.sources import pnnl
from atlas.sources.base import (
    Candidate,
    ImportContext,
    Importer,
    InputSnapshot,
    read_review_queue,
)
from atlas.sources.osm import (
    IMPORTER,
    MIN_ELEMENTS,
    OSM_SUPPORTS,
    OVERPASS_ENDPOINTS,
    OVERPASS_QUERY,
    BuildResult,
    OsmImporter,
    OverpassError,
    build_candidates,
    check_overpass,
    classify,
    fetch_overpass,
    osm_date,
    parse_mw,
    parse_overpass,
)
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OVERPASS_FIXTURE = FIXTURES / "osm" / "overpass-sample.json"
PNNL_GEOJSON = FIXTURES / "pnnl" / "centroids-sample.geojson"
OSM_BASE = "2026-10-07T22:19:21Z"
RETRIEVED = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 12)
ASHBURN = (39.0100, -77.4700)  # Loudoun County, VA


def fixture_doc() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(OVERPASS_FIXTURE.read_text(encoding="utf-8"))
    return doc


def snapshot(name: str = "overpass", upstream: str | None = OSM_BASE) -> InputSnapshot:
    return InputSnapshot(
        name=name,
        url=HttpUrl(OVERPASS_ENDPOINTS[1]),
        retrieved_at=RETRIEVED,
        sha256="0" * 64,
        bytes=1,
        license="ODbL-1.0",
        upstream_version=upstream,
    )


def build(
    counties: CountyIndex,
    objects: list[OsmObject] | None = None,
    *,
    with_pnnl: bool = True,
    existing: dict[str, FacilityRecord] | None = None,
) -> BuildResult:
    if objects is None:
        objects, _ = parse_overpass(fixture_doc())
    clusters = dissolve(objects)
    join = pnnl.join(clusters, pnnl.load_pnnl(PNNL_GEOJSON)) if with_pnnl else None
    return build_candidates(
        clusters,
        counties=counties,
        now=NOW,
        snapshot=snapshot(),
        pnnl_join=join,
        pnnl_snapshot=snapshot("pnnl", None) if with_pnnl else None,
        existing=existing,
    )


def candidate(result: BuildResult, ref: str) -> Candidate:
    (found,) = [c for c in result.candidates if ref in c.match_values]
    return found


def near(north_m: float, east_m: float) -> tuple[float, float]:
    dlat, dlon = meters_to_degrees(1.0, ASHBURN[0])
    return ASHBURN[0] + north_m * dlat, ASHBURN[1] + east_m * dlon


def building(ref: str, north_m: float, east_m: float, **tags: str) -> OsmObject:
    lat, lon = near(north_m, east_m)
    d = 0.0002
    tags = {"building": "data_center", "telecom": "data_center"} | {
        k.replace("__", ":"): v for k, v in tags.items()
    }
    bounds = Bounds(lat - d, lon - d, lat + d, lon + d)
    return OsmObject(ref, lat, lon, bounds, tags, classify(ref.split("/")[0], tags))


@pytest.fixture(scope="module")
def built(counties: CountyIndex) -> BuildResult:
    return build(counties)


# ---------------------------------------------------------------------------- parse


def test_parse_fixture() -> None:
    objects, skipped = parse_overpass(fixture_doc())
    assert skipped == []
    assert len(objects) == 30
    kinds = {o.ref: o.kind for o in objects}
    assert kinds["way/460053028"] == "campus"
    assert kinds["way/460053030"] == "campus"
    assert kinds["node/14156477686"] == "point"
    assert kinds["way/1560827027"] == "building"  # building=construction
    assert sorted(k for k in kinds.values()) == ["building"] * 24 + ["campus"] * 2 + ["point"] * 4
    campus = next(o for o in objects if o.ref == "way/460053028")
    assert campus.bounds == Bounds(39.0249298, -77.454534, 39.0277324, -77.448729)
    assert (campus.lat, campus.lon) == campus.bounds.center


def test_parse_variants() -> None:
    doc = {
        "elements": [
            {"type": "node", "id": 1, "lat": 39.0, "lon": -77.0, "tags": {"name": "A​ B "}},
            {"type": "node", "id": 1, "lat": 39.0, "lon": -77.0, "tags": {}},  # duplicate
            {"type": "way", "id": 2, "center": {"lat": 39.1, "lon": -77.1}, "tags": {}},
            {"type": "way", "id": 3, "tags": {"telecom": "data_center"}},  # no position
            {"type": "area", "id": 4},
            "junk",
        ]
    }
    objects, skipped = parse_overpass(doc)
    assert [o.ref for o in objects] == ["node/1", "way/2"]
    assert objects[0].tags == {"name": "A B"}
    assert objects[1].bounds is None and (objects[1].lat, objects[1].lon) == (39.1, -77.1)
    assert skipped == ["way/3"]


def test_classify() -> None:
    assert classify("node", {"telecom": "data_center"}) == "point"
    assert classify("way", {"telecom": "data_center"}) == "campus"
    assert classify("relation", {"telecom": "data_center", "building": "no"}) == "campus"
    assert classify("way", {"building": "no", "construction": "data_center"}) == "campus"
    assert classify("way", {"telecom": "data_center", "building": "yes"}) == "building"
    assert classify("way", {"building": "data_center"}) == "building"


def test_check_overpass() -> None:
    doc = fixture_doc()
    assert check_overpass(doc, min_elements=30) == OSM_BASE
    with pytest.raises(OverpassError, match="fewer than 31"):
        check_overpass(doc, min_elements=31)
    with pytest.raises(OverpassError, match="runtime error"):
        check_overpass(doc | {"remark": "runtime error: Query timed out"}, min_elements=0)
    with pytest.raises(OverpassError, match="timestamp_osm_base"):
        check_overpass({"elements": []}, min_elements=0)
    with pytest.raises(OverpassError, match="JSON object"):
        check_overpass([], min_elements=0)
    assert MIN_ELEMENTS == 1000


@pytest.mark.parametrize(
    ("value", "mw"),
    [
        ("36 MW", 36.0),
        ("2.0 MW", 2.0),
        ("12.6 MW", 12.6),
        ("36MW", 36.0),
        ("36 mw", 36.0),
        ("500 kW", 0.5),
        ("1.2 GW", 1200.0),
        ("45", 45.0),
        ("36 MVA", None),
        ("about 36 MW", None),
        ("36 MW;14 MW", None),
        ("36,5 MW", None),
        ("0 MW", None),
        ("20000 MW", None),
        ("", None),
    ],
)
def test_parse_mw(value: str, mw: float | None) -> None:
    assert parse_mw(value) == mw


def test_osm_date() -> None:
    latest = date(2026, 10, 7)
    assert osm_date("2019", latest=latest) is not None
    d = osm_date("2024-09-01", latest=latest)
    assert d is not None and (d.value, d.precision) == ("2024-09-01", "day")
    d = osm_date("2024-09", latest=latest)
    assert d is not None and d.precision == "month"
    for bad in ("1938", "2027", "~2019", "2019-13", "2019-02-30", "2019s", None, ""):
        assert osm_date(bad, latest=latest) is None


# ---------------------------------------------------------------------------- fetch


def overpass_response(doc: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "application/json"}, content=json.dumps(doc).encode()
    )


def test_overpass_failover(make_test_context: MakeContext) -> None:
    calls: list[str] = []
    bodies: list[bytes] = []
    good = fixture_doc()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        bodies.append(request.content)
        assert request.method == "POST"
        if url == OVERPASS_ENDPOINTS[0]:
            return httpx.Response(500, text="server too busy")
        if url == OVERPASS_ENDPOINTS[1]:
            return overpass_response(good | {"remark": "runtime error: Query timed out"})
        if url == OVERPASS_ENDPOINTS[2]:
            return overpass_response(good)
        raise AssertionError(f"unexpected request {url}")

    ctx = make_test_context(handler=handler)
    slept: list[float] = []
    result = fetch_overpass(ctx, OVERPASS_ENDPOINTS, min_elements=10, retries=1, sleep=slept.append)
    # endpoint 1: 500, retried once; endpoint 2: partial answer; endpoint 3: complete
    assert calls == [OVERPASS_ENDPOINTS[0], OVERPASS_ENDPOINTS[0], *OVERPASS_ENDPOINTS[1:3]]
    assert slept == [10.0]
    assert urllib.parse.parse_qs(bodies[-1].decode())["data"] == [OVERPASS_QUERY]
    assert str(result.snapshot.url) == OVERPASS_ENDPOINTS[2]
    assert result.snapshot.upstream_version == OSM_BASE
    assert result.snapshot.license == "ODbL-1.0"
    assert len(result.doc["elements"]) == 30
    raw = ctx.raw_path("overpass", result.snapshot.sha256, "json")
    assert json.loads(raw.read_bytes()) == good


def test_overpass_element_guard(make_test_context: MakeContext) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return overpass_response(fixture_doc())

    ctx = make_test_context(handler=handler)
    with pytest.raises(FetchError, match="every Overpass endpoint failed") as e:
        fetch_overpass(ctx, OVERPASS_ENDPOINTS, min_elements=31, sleep=lambda s: None)
    assert "30 elements, fewer than 31" in str(e.value)
    assert calls == list(OVERPASS_ENDPOINTS)
    calls.clear()
    fetch_overpass(ctx, OVERPASS_ENDPOINTS, min_elements=30, sleep=lambda s: None)
    assert calls == [OVERPASS_ENDPOINTS[0]]


def test_overpass_bad_content(make_test_context: MakeContext) -> None:
    answers = iter(
        [
            httpx.Response(200, headers={"content-type": "text/html"}, text="<html>busy</html>"),
            httpx.Response(200, headers={"content-type": "application/json"}, text="{not json"),
            overpass_response({"elements": []}),
            overpass_response(fixture_doc()),
        ]
    )
    ctx = make_test_context(handler=lambda request: next(answers))
    result = fetch_overpass(ctx, OVERPASS_ENDPOINTS, min_elements=1, sleep=lambda s: None)
    assert str(result.snapshot.url) == OVERPASS_ENDPOINTS[3]


def test_overpass_input_file(make_test_context: MakeContext) -> None:
    ctx = make_test_context(input_path=OVERPASS_FIXTURE)
    result = fetch_overpass(ctx, OVERPASS_ENDPOINTS)  # no element minimum for a saved file
    assert result.snapshot.retrieved_at == ctx.now
    assert result.snapshot.upstream_version == OSM_BASE
    assert str(result.snapshot.url) == OVERPASS_ENDPOINTS[0]


# ---------------------------------------------------------------------------- records


AWS_REFS = (
    "way/460053028",
    "way/460053030",
    "way/463571875",
    "way/463571876",
    "way/556599693",
    "way/556599694",
    "way/556599695",
    "way/596690174",
)


def test_campus_record(built: BuildResult) -> None:
    # The two AWS campus polygons and their six buildings: IAD-80's box lies within 300 m of
    # IAD-71's, so the campuses form one site. The larger campus, way/460053030, represents it.
    c = candidate(built, "way/460053028")
    r = c.record
    assert c.match_values == AWS_REFS
    assert r.id == PLACEHOLDER_ID
    assert r.record_type == "campus" and r.scope == "in_scope"
    assert r.canonical_name == "Amazon Web Services (Ashburn, VA)"
    assert [a.name for a in r.aliases] == [
        "Amazon Web Services Datacenter Complex",
        "Amazon IAD-78",
        "Amazon IAD-79",
        "Amazon IAD-71",
        "Amazon IAD-50",
        "Amazon IAD-60",
        "Amazon IAD-80",
    ]
    assert all(a.kind == "osm_name" and a.source_ids == ["s1"] for a in r.aliases)
    assert [(o.name, o.source_ids) for o in r.parties.operator] == [("Amazon Web Services", ["s1"])]
    assert r.parties.owner == []

    rep = next(o for o in parse_overpass(fixture_doc())[0] if o.ref == "way/460053030")
    assert rep.bounds is not None
    loc = r.location
    assert (loc.lat, loc.lon) == (round(rep.lat, 7), round(rep.lon, 7))
    assert loc.precision == "footprint" and loc.geocode_method == "osm"
    assert loc.geometry_ref == "osm:way/460053030"
    assert (loc.county_fips, loc.county_name, loc.state_abbr) == ("51107", "Loudoun", "VA")
    assert (loc.street, loc.city, loc.postcode) == (
        "44862 Interconnection Plaza",
        "Ashburn",
        "20147",
    )

    assert r.capacity.it_mw == 245.0  # 45 + 47 + 28 + 59 + 28 + 38
    assert r.capacity.facility_mw == 260.0  # 45 + 45 + 32.5 + 65 + 32.5 + 40
    assert r.capacity.mw_as_stated == (
        "OSM it_power: Amazon IAD-78 45 MW; Amazon IAD-79 47 MW; Amazon IAD-71 28 MW; "
        "Amazon IAD-50 59 MW; Amazon IAD-60 28 MW; Amazon IAD-80 38 MW | "
        "OSM input:electricity: Amazon IAD-78 45 MW; Amazon IAD-79 45 MW; "
        "Amazon IAD-71 32.5 MW; Amazon IAD-50 65 MW; Amazon IAD-60 32.5 MW; Amazon IAD-80 40 MW"
    )
    assert [(b.ref, b.name, b.sqft) for b in r.buildings] == [
        ("osm:way/463571875", "Amazon IAD-78", 147979.0),
        ("osm:way/463571876", "Amazon IAD-79", 148678.0),
        ("osm:way/556599693", "Amazon IAD-71", 121176.0),
        ("osm:way/556599694", "Amazon IAD-50", None),
        ("osm:way/556599695", "Amazon IAD-60", None),
        ("osm:way/596690174", "Amazon IAD-80", 150443.0),
    ]
    assert r.site.building_sqft == 568276.0 and r.site.acreage is None

    # The campus has no start_date and the earliest building's is 2015 (IAD-50): energized 2015.
    (event,) = r.status_history
    assert (event.seq, event.status, event.event, event.planned) == (
        1,
        "operating",
        "energized",
        False,
    )
    assert (event.as_of.value, event.as_of.precision) == ("2015", "year")
    assert event.source_ids == ["s1"]
    assert (
        event.note
        == "start_date=2015 on way/556599694; tagged telecom=data_center in OpenStreetMap"
    )
    assert r.status == "operating" and r.evidence_level == "reported" and r.purpose == "unknown"
    assert {k: v.value for k, v in r.dates.items()} == {
        "first_reported": "2015",
        "operating_since": "2015",
    }

    assert r.external_ids == {
        "osm": list(c.match_values),
        "pnnl_im3": [
            "building@-77.449520,39.026368",
            "building@-77.450870,39.026357",
            "building@-77.452829,39.027109",
            "building@-77.457503,39.027908",
        ],
    }
    s1, s2 = r.sources
    assert (s1.id, str(s1.url), s1.publisher) == (
        "s1",
        "https://www.openstreetmap.org/way/460053030",
        "OpenStreetMap contributors",
    )
    assert (s1.title, s1.source_type, s1.license) == (
        "OpenStreetMap way/460053030",
        "open_dataset",
        "ODbL-1.0",
    )
    assert s1.supports == list(OSM_SUPPORTS) and s1.retrieved_at == RETRIEVED
    assert (s2.id, str(s2.url), s2.license) == (
        "s2",
        "https://doi.org/10.57931/3017294",
        "ODbL-1.0",
    )
    assert s2.title == "IM3 Open Source Data Center Atlas v2026.02.09"
    assert s2.publisher == "Pacific Northwest National Laboratory (IM3)"
    assert s2.supports == ["/site", "/buildings"]

    meta = {k: (v.confidence, v.method, v.source_ids) for k, v in r.field_meta.items()}
    assert meta == {
        "/capacity/facility_mw": (0.7, "imported", ["s1"]),
        "/capacity/it_mw": (0.7, "imported", ["s1"]),
        "/location/county_fips": (0.9, "derived", []),
        "/status": (0.6, "imported", ["s1"]),
    }
    assert r.review.state == "machine"
    assert r.created_at == r.updated_at == r.last_verified_at == NOW


def test_first_reported_at_snapshot_date(built: BuildResult) -> None:
    r = candidate(built, "way/300162689").record  # Lumen Ashburn: no start_date
    (event,) = r.status_history
    assert (event.event, event.as_of.value, event.as_of.precision) == (
        "first_reported",
        "2026-10-07",
        "day",
    )
    assert event.note == "Tagged telecom=data_center in OpenStreetMap; status per 07 §4.7"


def test_names(built: BuildResult) -> None:
    names = {c.match_values[0]: c.record.canonical_name for c in built.candidates}
    assert names["way/300162689"] == "Lumen Ashburn (Ashburn, VA)"  # operator Lumen Technologies
    assert names["way/1188715508"] == "PowerHouse CyrusOne NVA14 (Ashburn, VA)"
    assert names["node/11721960464"] == "QTS Data Center - Hillsboro 3 (Washington, OR)"
    assert names["node/13154826379"] == "NTT VA11 (Gainesville, VA)"
    equinix = candidate(built, "node/14156477686").record
    assert equinix.canonical_name == "Equinix DC10 (Ashburn, VA)"
    assert "Equinix Ashburn DC18" in [a.name for a in equinix.aliases]
    assert equinix.location.precision == "footprint"


def test_status_mapping(built: BuildResult) -> None:
    qts = candidate(built, "node/11721960464").record
    assert qts.status == "proposed" and qts.location.precision == "site"
    assert qts.status_history[0].note == (
        "Tagged proposed:telecom=data_center in OpenStreetMap; status per 07 §4.7"
    )
    ntt = candidate(built, "node/13154826379").record
    assert ntt.status == "under_construction"
    assert (
        ntt.capacity.it_mw == 96.0 and ntt.capacity.mw_as_stated == "OSM it_power: NTT VA11 96 MW"
    )
    assert [b.ref for b in ntt.buildings] == ["osm:node/13154826379"]


def test_mixed_statuses_become_phases(built: BuildResult) -> None:
    r = candidate(built, "way/1560827027").record  # Cologix ASH1 (operating) + ASH2 (construction)
    assert r.canonical_name == "Cologix ASH1 (Ashburn, VA)"
    assert [(p.phase_id, p.name, p.source_ids) for p in r.phases] == [
        ("osm-operating", "Operating in OpenStreetMap: Cologix ASH1", ["s1"]),
        ("osm-under_construction", "Under construction in OpenStreetMap: Cologix ASH2", ["s1"]),
    ]
    assert [(e.seq, e.status, e.phase_id) for e in r.status_history] == [
        (1, "operating", "osm-operating"),
        (2, "under_construction", "osm-under_construction"),
    ]
    assert r.status_history[1].note == (
        "Tagged construction=data_center in OpenStreetMap; status per 07 §4.7"
    )
    assert [(b.ref, b.phase_id) for b in r.buildings] == [
        ("osm:way/1188715510", "osm-operating"),
        ("osm:way/1560827027", "osm-under_construction"),
    ]
    assert r.status == "operating"
    assert phase_statuses(r) == {
        "osm-operating": "operating",
        "osm-under_construction": "under_construction",
    }
    assert is_expanding(r)


def test_out_of_scope(built: BuildResult) -> None:
    c = candidate(built, "node/10014176940")
    r = c.record
    assert r.scope == "out_of_scope"
    assert r.canonical_name == "Microsoft (Guaynabo, PR)"
    assert (r.location.state_abbr, r.location.county_fips) == ("PR", "72061")
    assert "787" not in json.dumps(record_json(r))  # the phone tag is never copied
    (item,) = [i for i in built.review if i.kind == "out_of_scope"]
    assert (item.source, item.external_id) == ("osm", "node/10014176940")


def test_metrics(built: BuildResult) -> None:
    assert built.metrics == {
        "objects": 30,
        "clusters": 12,
        "out_of_scope": 1,
        "unit_parse": 0,
        "pnnl_rows": 17,
        "pnnl_matched": 16,
        "pnnl_matched_by_id": 0,
        "pnnl_match_rate": 0.9412,
    }
    assert sorted((i.source, i.kind) for i in built.review) == [
        ("osm", "out_of_scope"),
        ("pnnl", "unmatched"),
    ]


def test_every_candidate_validates(built: BuildResult, counties: CountyIndex) -> None:
    assert len(built.candidates) == 12
    for c in built.candidates:
        assert validate_record(c.record, counties=counties, today=TODAY) == [], c.match_values
        assert FacilityRecord.model_validate(record_json(c.record)) == c.record


def test_without_pnnl(counties: CountyIndex) -> None:
    result = build(counties, with_pnnl=False)
    r = candidate(result, "way/460053028").record
    assert [s.id for s in r.sources] == ["s1"]
    assert "pnnl_im3" not in r.external_ids
    assert [b.sqft for b in r.buildings] == [None] * 6
    assert r.site.building_sqft is None
    assert "pnnl_rows" not in result.metrics


# ---------------------------------------------------------------------------- synthetic clusters


def test_unit_parse_and_partial_power(counties: CountyIndex) -> None:
    a = building("way/1", 0, 0, name="Hall A", operator="Example", it_power="36 MVA")
    b = building("way/2", 0, 100, name="Hall B", operator="Example", it_power="10 MW")
    c = building("way/3", 0, 200, name="Hall C", operator="Example", input__electricity="5 MW")
    result = build(counties, [a, b, c], with_pnnl=False)
    (cand,) = result.candidates
    cap = cand.record.capacity
    assert cap.it_mw is None and cap.facility_mw is None  # not every building has a usable value
    assert cap.mw_as_stated == (
        "OSM it_power: Hall A 36 MVA; Hall B 10 MW | OSM input:electricity: Hall C 5 MW"
    )
    assert "/capacity/it_mw" not in cand.record.field_meta
    (item,) = [i for i in result.review if i.kind == "unit_parse"]
    assert (item.external_id, item.data["value"]) == ("way/1", "36 MVA")
    assert result.metrics["unit_parse"] == 1


def test_power_units(counties: CountyIndex) -> None:
    a = building("way/1", 0, 0, operator="Example", it_power="500 kW")
    b = building("way/2", 0, 100, operator="Example", it_power="1.5")
    (cand,) = build(counties, [a, b], with_pnnl=False).candidates
    assert cand.record.capacity.it_mw == 2.0


def test_start_date_rules(counties: CountyIndex) -> None:
    old = building("way/1", 0, 0, name="Old", start_date="1938")
    month = building("way/2", 0, 2000, name="Month", start_date="2019-05")
    future = building("way/3", 0, 4000, name="Future", start_date="2027")
    built_ = build(counties, [old, month, future], with_pnnl=False)
    events = {c.match_values[0]: c.record.status_history[0] for c in built_.candidates}
    assert (events["way/1"].event, events["way/1"].as_of.value) == ("first_reported", "2026-10-07")
    assert (events["way/2"].event, events["way/2"].as_of.value) == ("energized", "2019-05")
    assert events["way/2"].as_of.precision == "month"
    assert (events["way/3"].event, events["way/3"].as_of.value) == ("first_reported", "2026-10-07")


def test_opening_date_is_planned(counties: CountyIndex) -> None:
    site = building(
        "way/1",
        0,
        0,
        name="Future Hall",
        building="construction",
        construction="data_center",
        opening_date="2028",
    )
    (cand,) = build(counties, [site], with_pnnl=False).candidates
    r = cand.record
    assert [(e.seq, e.status, e.event, e.planned, e.as_of.value) for e in r.status_history] == [
        (1, "under_construction", "first_reported", False, "2026-10-07"),
        (2, "operating", "energized", True, "2028"),
    ]
    assert r.status == "under_construction"
    assert "operating_since" not in r.dates
    assert validate_record(r, counties=counties, today=TODAY) == []


def test_unnamed_cluster_names(counties: CountyIndex) -> None:
    bare = building("way/1", 0, 0)
    op = building("way/2", 0, 3000, operator="Example Cloud")
    names = {
        c.match_values[0]: c.record.canonical_name
        for c in build(counties, [bare, op], with_pnnl=False).candidates
    }
    assert names == {
        "way/1": "Data center (Loudoun, VA)",
        "way/2": "Example Cloud data center (Loudoun, VA)",
    }


def test_existing_first_reported_date_is_kept(counties: CountyIndex) -> None:
    first = candidate(build(counties), "way/300162689").record
    as_of = FuzzyDate(value="2026-01", precision="month")
    events = [first.status_history[0].model_copy(update={"as_of": as_of})]
    stored = first.model_copy(
        update={"id": "gwa-01m4c7rym8gsp21hkfp8nhzxz1", "status_history": events}
    )
    again = candidate(build(counties, existing={stored.id: stored}), "way/300162689").record
    assert again.status_history[0].as_of.value == "2026-01"


# ---------------------------------------------------------------------------- importer and CLI


def test_importer_is_discovered() -> None:
    importer: Importer = IMPORTER
    found = discover_importers()
    assert found["osm"] is importer
    assert "pnnl" not in found
    assert importer.match_key == "osm"
    assert importer.owned_external_keys == ("osm", "pnnl_im3")
    assert importer.review_sources == ("osm", "pnnl")
    assert importer.version == "1"


def parse_args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    IMPORTER.add_arguments(parser)
    return parser.parse_args(argv)


def test_arguments() -> None:
    args = parse_args()
    assert (args.pnnl, args.no_pnnl, args.overpass_url, args.dissolve_m) == (
        None,
        False,
        None,
        300.0,
    )
    args = parse_args(
        "--overpass-url", "https://a.example/api", "--overpass-url", "https://b.example/api"
    )
    assert args.overpass_url == ["https://a.example/api", "https://b.example/api"]
    with pytest.raises(SystemExit):
        parse_args("--pnnl", "x.geojson", "--no-pnnl")
    assert parse_args("--dissolve-m", "150").dissolve_m == 150.0
    for bad in ("0", "-5", "abc", "9000"):
        with pytest.raises(SystemExit):
            parse_args("--dissolve-m", bad)


def test_run_uses_overpass_urls(make_test_context: MakeContext) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return overpass_response(fixture_doc())

    ctx = make_test_context(handler=handler)
    importer = OsmImporter(min_elements=1, sleep=lambda s: None)
    result = importer.run(
        ctx, parse_args("--overpass-url", "https://overpass.example/api/interpreter", "--no-pnnl")
    )
    assert calls == ["https://overpass.example/api/interpreter"]
    assert [i.name for i in result.inputs] == ["overpass"]
    assert result.metrics["clusters"] == 12


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_cli_import_is_idempotent(tmp_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "import",
        "osm",
        "--input",
        str(OVERPASS_FIXTURE),
        "--pnnl",
        str(PNNL_GEOJSON),
        "--now",
        "2026-10-12T00:00:00Z",
        "--records",
        str(tmp_repo / "data" / "records"),
        "--review-dir",
        str(tmp_repo / "review" / "queue"),
        "--receipts-dir",
        str(tmp_repo / "data" / "imports"),
        "--cache-dir",
        str(tmp_repo / ".cache" / "atlas"),
        "--offline",
    ]
    assert main(argv) == 0
    out = capsys.readouterr()
    assert "candidates=12 new=12" in out.out
    assert "warning: 94.1% of PNNL rows matched" in out.err
    before = tree(tmp_repo)
    assert len([k for k in before if k.startswith("data/records/")]) == 12
    receipt = json.loads(before["data/imports/osm.json"])
    assert receipt["counts"]["invalid"] == 0
    assert [i["name"] for i in receipt["inputs"]] == ["overpass", "pnnl"]
    assert receipt["inputs"][0]["upstream_version"] == OSM_BASE
    assert {i.kind for i in read_review_queue(tmp_repo / "review" / "queue", "pnnl")} == {
        "unmatched"
    }

    # Run 2 rewrites nothing but the receipt, whose counts now say unchanged; run 3 changes nothing.
    assert main(argv) == 0
    out = capsys.readouterr()
    assert "new=0 updated=0 unchanged=12" in out.out
    second = tree(tmp_repo)
    changed = sorted(k for k in second if second[k] != before.get(k))
    assert changed == ["data/imports/osm.json"]
    assert set(second) == set(before)
    assert main(argv) == 0
    assert tree(tmp_repo) == second


def test_cli_offline_without_input_fails(
    tmp_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "import",
            "osm",
            "--no-pnnl",
            "--records",
            str(tmp_repo / "data" / "records"),
            "--review-dir",
            str(tmp_repo / "review" / "queue"),
            "--receipts-dir",
            str(tmp_repo / "data" / "imports"),
            "--cache-dir",
            str(tmp_repo / ".cache" / "atlas"),
            "--offline",
        ]
    )
    assert code == 1
    assert "every Overpass endpoint failed" in capsys.readouterr().err
    assert list((tmp_repo / "data" / "records").iterdir()) == []


@pytest.mark.network
def test_live_overpass(make_test_context: MakeContext) -> None:
    """The full US query on maps.mail.ru (the endpoint that answered from the sandbox)."""
    with make_client() as http:
        ctx = make_test_context(http=http)
        result = fetch_overpass(
            ctx, ["https://maps.mail.ru/osm/tools/overpass/api/interpreter"], retries=1
        )
    assert 1_500 <= len(result.doc["elements"]) <= 2_500
