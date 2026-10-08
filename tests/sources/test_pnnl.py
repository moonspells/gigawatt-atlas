from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import HttpUrl

from atlas.dissolve import Bounds, Cluster, ObjectKind, OsmObject, dissolve
from atlas.geo.counties import CountyIndex
from atlas.net import FetchError
from atlas.sources import pnnl
from atlas.sources.base import ImportContext, InputSnapshot
from atlas.sources.osm import OsmImporter, build_candidates, parse_overpass
from atlas.sources.pnnl import PnnlRow

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OVERPASS_FIXTURE = FIXTURES / "osm" / "overpass-sample.json"
OVERPASS_CASES = FIXTURES / "osm" / "overpass-cases.json"
GEOJSON = FIXTURES / "pnnl" / "centroids-sample.geojson"
GEOJSON_CASES = FIXTURES / "pnnl" / "centroids-cases.geojson"
CSV = FIXTURES / "pnnl" / "msdlive-sample.csv"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
UNMATCHED_KEY = "building@-77.459006,39.016471"  # Equinix Ashburn DC2, outside the OSM sample


def clusters(path: Path = OVERPASS_FIXTURE) -> list[Cluster]:
    objects, _ = parse_overpass(json.loads(path.read_text(encoding="utf-8")))
    return dissolve(objects)


def names_by_rep(result: pnnl.JoinResult) -> dict[str, list[str]]:
    return {rep: sorted(r.name or "" for r in rows) for rep, rows in result.matched.items()}


def snapshot(name: str) -> InputSnapshot:
    return InputSnapshot(
        name=name,
        url=HttpUrl(pnnl.PNNL_GEOJSON_URL),
        retrieved_at=NOW,
        sha256="0" * 64,
        bytes=1,
        license="ODbL-1.0",
        upstream_version="2026-10-07T22:19:21Z" if name == "overpass" else None,
    )


# ---------------------------------------------------------------------------- parse


def test_parse_geojson() -> None:
    rows = pnnl.load_pnnl(GEOJSON)
    assert len(rows) == 17
    first = rows[0]
    assert (first.type, first.name, first.operator, first.state_abb, first.county, first.sqft) == (
        "building",
        "Digital Realty ACC4",
        "Digital Realty",
        "VA",
        "Loudoun County",
        294156.0,
    )
    assert (first.lat, first.lon) == pytest.approx((39.017902, -77.462549), abs=1e-6)
    assert first.osm_id is None and first.county_id is None
    assert first.key == "building@-77.462549,39.017902"
    assert first.osm_refs() == ()
    unnamed = next(r for r in rows if r.name is None)
    assert unnamed.operator is None and unnamed.sqft == 158127.0


def test_control_characters_are_dropped_before_names_are_compared() -> None:
    # PNNL names are matched against OSM tags, which go through the same clean_text.
    assert pnnl._clean("Micro\x7fsoft\x1b ") == "Microsoft"
    assert pnnl._clean("\x07") is None


def test_parse_csv() -> None:
    rows = pnnl.load_pnnl(CSV)
    assert len(rows) == 17
    first = rows[0]
    assert (first.osm_id, first.county_id, first.ref, first.key) == (
        "298126010",
        "51107",
        "ACC4",
        "building:298126010",
    )
    assert first.osm_refs() == ("way/298126010", "relation/298126010")
    assert PnnlRow(type="point", lat=0, lon=0, osm_id="7").osm_refs() == ("node/7",)
    # The CSV rows are the GeoJSON rows plus ids.
    geo = pnnl.load_pnnl(GEOJSON)
    assert [(r.name, r.sqft, r.type) for r in rows] == [(r.name, r.sqft, r.type) for r in geo]
    assert all(
        abs(a.lat - b.lat) < 1e-7 and abs(a.lon - b.lon) < 1e-7
        for a, b in zip(rows, geo, strict=True)
    )


def test_parse_csv_variants() -> None:
    text = "type,lat,lon,id,county_id,sqft\nBuilding,39.0,-77.0,123.0,1001,\n"
    (row,) = pnnl.parse_pnnl(text.encode("utf-8-sig"))
    assert (row.type, row.osm_id, row.county_id, row.sqft) == ("building", "123", "01001", None)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b'{"type": "Feature"}', "not a FeatureCollection"),
        (b'{"type": "FeatureCollection", "features": [{"geometry": null}]}', "not a Point"),
        (
            b'{"type": "FeatureCollection", "features": [{"geometry": {"type": "Point",'
            b' "coordinates": [-77, 39]}, "properties": {"type": "silo"}}]}',
            "type 'silo'",
        ),
        (b'{"type": "FeatureCollection", "features": []}', "no rows"),
        (b"name,lat\nA,39\n", "missing column"),
        (b"type,lat,lon,id\nbuilding,39,-77,way/1\n", "not an OSM id"),
        (b"type,lat,lon\nbuilding,,-77\n", "lat and lon"),
    ],
)
def test_parse_errors(data: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        pnnl.parse_pnnl(data)


# ---------------------------------------------------------------------------- join


def test_spatial_join() -> None:
    result = pnnl.join(clusters(), pnnl.load_pnnl(GEOJSON))
    assert [r.key for r in result.unmatched] == [UNMATCHED_KEY]
    assert result.total == 17 and result.by_id == 0
    assert result.match_rate == pytest.approx(16 / 17)
    assert names_by_rep(result)["way/460053030"] == [
        "Amazon IAD71",
        "Amazon IAD78",
        "Amazon IAD79",
        "Amazon IAD80",
    ]
    assert result.members["building@-77.449520,39.026368"] == "way/463571875"
    # The unnamed PNNL row lies in Cologix ASH1's footprint.
    assert result.members["building@-77.459474,39.017948"] == "way/1188715510"


def test_id_join_matches_the_spatial_join() -> None:
    cl = clusters()
    spatial = pnnl.join(cl, pnnl.load_pnnl(GEOJSON))
    by_id = pnnl.join(cl, pnnl.load_pnnl(CSV))
    assert by_id.by_id == 16
    assert names_by_rep(by_id) == names_by_rep(spatial)
    (gone,) = by_id.unmatched
    assert gone.key == "building:234722029"
    assert "OSM no longer has way/234722029" in pnnl.unmatched_item(gone).reason


def test_id_join_does_not_fall_back_to_space() -> None:
    # A row with an id that OSM no longer has stays unmatched, even on top of another building.
    row = PnnlRow(type="building", lat=39.0263680, lon=-77.4495202, osm_id="1")
    result = pnnl.join(clusters(), [row])
    assert result.unmatched == [row]


def test_id_join_campus_relation() -> None:
    bounds = Bounds(39.0, -77.5, 39.01, -77.49)
    campus = OsmObject("relation/77", *bounds.center, bounds, {"telecom": "data_center"}, "campus")
    result = pnnl.join(dissolve([campus]), [PnnlRow(type="campus", lat=0, lon=0, osm_id="77")])
    assert result.matched == {"relation/77": [PnnlRow(type="campus", lat=0, lon=0, osm_id="77")]}


def obj(ref: str, lat: float, lon: float, **tags: str) -> OsmObject:
    d = 0.0002
    return OsmObject(ref, lat, lon, Bounds(lat - d, lon - d, lat + d, lon + d), tags, "building")


def test_spatial_tie_breaks() -> None:
    lat, lon = 39.0, -77.5
    near = obj("way/1", lat, lon, name="Hall One", operator="Alpha")
    named = obj("way/2", lat + 0.0003, lon, name="Hall Two", operator="Beta")
    run = obj("way/3", lat - 0.0003, lon, name="Other", operator="Beta Inc")
    members = [near, named, run]
    # The point is 11 m from way/1, 22 m from way/2 and 44 m from way/3: all within reach.

    def at(name: str | None = None, operator: str | None = None) -> PnnlRow:
        return PnnlRow(type="building", lat=lat + 0.0001, lon=lon, name=name, operator=operator)

    assert pnnl.spatial_match(at(name="hall two"), members) is named
    assert pnnl.spatial_match(at(operator="BETA, LLC"), members) is named
    assert pnnl.spatial_match(at(), members) is near
    far = PnnlRow(type="point", lat=lat + 0.01, lon=lon)
    assert pnnl.spatial_match(far, members) is None


def test_within_50_m_of_a_node() -> None:
    node = OsmObject("node/5", 39.0, -77.5, None, {"telecom": "data_center"}, "point")
    close = PnnlRow(type="point", lat=39.0004, lon=-77.5)  # 44 m
    far = PnnlRow(type="point", lat=39.0005, lon=-77.5)  # 56 m
    result = pnnl.join(dissolve([node]), [close, far])
    assert result.matched == {"node/5": [close]} and result.unmatched == [far]


# ---------------------------------------------------------------------------- effects


def test_effects_on_records(counties: CountyIndex) -> None:
    cl = clusters()
    join = pnnl.join(cl, pnnl.load_pnnl(GEOJSON))
    built = build_candidates(
        cl,
        counties=counties,
        now=NOW,
        snapshot=snapshot("overpass"),
        pnnl_join=join,
        pnnl_snapshot=snapshot("pnnl"),
    )
    by_rep = {c.record.location.geometry_ref: c.record for c in built.candidates}
    cologix = by_rep["osm:way/1188715510"]
    assert [(b.ref, b.sqft) for b in cologix.buildings] == [
        ("osm:way/1188715510", 158127.0),
        ("osm:way/1560827027", None),
    ]
    assert cologix.site.building_sqft == 158127.0
    assert cologix.external_ids["pnnl_im3"] == ["building@-77.459474,39.017948"]
    unmatched = [i for i in built.review if i.kind == "unmatched"]
    assert [(i.source, i.external_id, i.record_id) for i in unmatched] == [
        ("pnnl", UNMATCHED_KEY, None)
    ]
    assert unmatched[0].data["name"] == "Equinix Ashburn DC2"
    assert unmatched[0].data["sqft"] == 175215.0
    assert not [i for i in built.review if i.kind == "county_mismatch"]


def test_site_values() -> None:
    def way(ref: str, kind: ObjectKind, lat: float, size: float, **tags: str) -> OsmObject:
        bounds = Bounds(lat, -77.5, lat + size, -77.5 + size)
        return OsmObject(ref, *bounds.center, bounds, tags, kind)

    hall = way("way/1", "building", 39.0, 0.001, name="Hall One")  # about 103,000 sq ft
    small = way("way/2", "building", 39.01, 0.0002)  # about 4,100 sq ft
    campus = way("way/3", "campus", 39.02, 0.005)
    node = OsmObject("node/4", 39.03, -77.5, None, {}, "point")
    cluster = Cluster(members=(node, hall, small, campus), representative=campus)

    def at(m: OsmObject, typ: str, sqft: float, name: str | None = None, d: float = 0) -> PnnlRow:
        return PnnlRow(type=typ, lat=m.lat + d, lon=m.lon, name=name, sqft=sqft)

    named = at(hall, "building", 90_000.0, name="hall one", d=0.0002)
    nearer = at(hall, "building", 80_000.0)  # nearer, but the other row has the hall's name
    rows = [
        named,
        named,  # the same key twice (a county-line site): counted once
        nearer,
        at(small, "building", 9_000.0),  # more than the small box: another footprint
        at(campus, "building", 1_000.0),  # on the campus object: site total only
        at(campus, "campus", 435_600.0),  # 10 acres
        at(small, "campus", 87_120.0, d=0.00001),  # a campus row on a building: held
        at(node, "building", 500.0),
        PnnlRow(type="point", lat=node.lat, lon=node.lon),
    ]
    members = {
        r.key: m.ref
        for r, m in zip(
            rows, [hall, hall, hall, small, campus, campus, small, node, node], strict=True
        )
    }
    values = pnnl.site_values(cluster, rows, members)
    assert values.building_sqft == {"way/1": 90_000.0, "node/4": 500.0}
    assert values.site_sqft == 91_500.0
    assert values.acreage == 10.0
    held = sorted((i.kind, i.external_id, i.data["osm"]) for i in values.review)
    assert held == sorted(
        [
            ("possible_duplicate", nearer.key, "way/1"),
            ("conflict", rows[3].key, "way/2"),
            ("conflict", rows[6].key, "way/2"),
        ]
    )
    assert all(i.source == "pnnl" and i.record_id is None for i in values.review)
    assert pnnl.site_values(cluster, [], {}) == pnnl.SiteValues()


def test_case_fixture_effects(counties: CountyIndex) -> None:
    cl = clusters(OVERPASS_CASES)
    join = pnnl.join(cl, pnnl.load_pnnl(GEOJSON_CASES))
    assert join.match_rate == 1.0
    built = build_candidates(
        cl,
        counties=counties,
        now=NOW,
        snapshot=snapshot("overpass"),
        pnnl_join=join,
        pnnl_snapshot=snapshot("pnnl"),
    )
    by_ref = {c.match_values[0]: c.record for c in built.candidates}
    # Apple Data Center: PNNL has the current footprint and an older one; only the named row counts.
    apple = by_ref["way/300974499"]
    assert [b.sqft for b in apple.buildings] == [1_338_261.0]
    assert apple.site.building_sqft == 1_338_261.0
    # Campus rows whose polygons left OpenStreetMap give no acreage to the buildings they hit.
    assert by_ref["way/844352473"].site.acreage is None  # Google, Douglas County, GA
    assert by_ref["way/1422191468"].site.acreage is None  # Dickey County, ND
    # Digital Realty ATL11 is listed in Douglas and Cobb County; one of them agrees.
    assert by_ref["way/975064000"].location.county_fips == "13097"
    items = sorted(
        (i.kind, i.external_id, i.data.get("osm")) for i in built.review if i.source == "pnnl"
    )
    assert items == [
        ("conflict", "campus@-84.585223,33.750590", "way/844352473"),
        ("conflict", "campus@-98.572441,46.013549", "way/1422191472"),
        ("possible_duplicate", "building@-111.604139,33.347021", "way/300974499"),
    ]


def test_county_mismatch(counties: CountyIndex) -> None:
    def check(county: str | None = None, county_id: str | None = None) -> str | None:
        row = PnnlRow(
            type="building",
            lat=39.02,
            lon=-77.45,
            state_abb="VA",
            county=county,
            county_id=county_id,
        )
        return pnnl.county_mismatch(row, county_fips="51107", state_abbr="VA", counties=counties)

    assert check(county="Loudoun County") is None
    assert check(county="Loudoun") is None
    assert check(county_id="51107") is None
    reason = check(county="Fairfax County")
    assert reason is not None and "Fairfax" in reason and "51107" in reason
    assert check(county_id="51059") is not None
    assert check(county="Nowhere County") is not None
    no_county = PnnlRow(type="building", lat=0, lon=0)
    assert (
        pnnl.county_mismatch(no_county, county_fips="51107", state_abbr="VA", counties=counties)
        is None
    )


def test_county_mismatch_review_item(counties: CountyIndex) -> None:
    cl = clusters()
    row = PnnlRow(
        type="building",
        lat=39.0263680,
        lon=-77.4495202,
        name="Amazon IAD78",
        state_abb="VA",
        county="Fairfax County",
        sqft=1.0,
    )
    built = build_candidates(
        cl,
        counties=counties,
        now=NOW,
        snapshot=snapshot("overpass"),
        pnnl_join=pnnl.join(cl, [row]),
        pnnl_snapshot=snapshot("pnnl"),
    )
    (item,) = [i for i in built.review if i.kind == "county_mismatch"]
    assert (item.source, item.external_id) == ("pnnl", row.key)
    assert item.data["osm"] == "way/460053030" and item.data["county_fips"] == "51107"
    assert built.metrics["pnnl_match_rate"] == 1.0


def test_county_mismatch_by_key(counties: CountyIndex) -> None:
    def row(county: str, lat: float = 39.02) -> PnnlRow:
        return PnnlRow(type="building", lat=lat, lon=-77.45, state_abb="VA", county=county)

    def check(rows: list[PnnlRow]) -> list[str]:
        found = pnnl.county_mismatches(
            rows, county_fips="51107", state_abbr="VA", counties=counties
        )
        return [r.key for r, _ in found]

    # PNNL lists a site that straddles a county line once per county: one row agrees.
    assert check([row("Fairfax County"), row("Loudoun County")]) == []
    assert check([row("Fairfax County"), row("Prince William County")]) == [row("").key]
    other = row("Fairfax County", lat=39.03)
    assert check([row("Loudoun County"), other]) == [other.key]


# ---------------------------------------------------------------------------- input and run


def importer_args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    return parser.parse_args(argv)


def test_run_reports_match_rate(
    make_test_context: MakeContext, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = make_test_context(input_path=OVERPASS_FIXTURE)
    result = OsmImporter().run(ctx, importer_args("--pnnl", str(CSV)))
    assert result.metrics["pnnl_match_rate"] == 0.9412
    assert result.metrics["pnnl_matched_by_id"] == 16
    assert "below the 95% M1 acceptance threshold" in capsys.readouterr().err
    pnnl_input = result.inputs[1]
    assert (pnnl_input.name, str(pnnl_input.url), pnnl_input.license) == (
        "pnnl",
        "https://doi.org/10.57931/3017294",
        "ODbL-1.0",
    )
    assert pnnl_input.retrieved_at == ctx.now
    keys = {k for c in result.candidates for k in c.record.external_ids.get("pnnl_im3", [])}
    assert "building:463571875" in keys


def pnnl_handler(
    calls: list[str], *, version_status: int = 200, latest: str = "v2026.02.09"
) -> Callable[[httpx.Request], httpx.Response]:
    geojson = GEOJSON.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == pnnl.MSDLIVE_RECORD + "/versions/latest":
            if version_status != 200:
                return httpx.Response(version_status)
            return httpx.Response(301, headers={"location": pnnl.MSDLIVE_RECORD})
        if url == pnnl.MSDLIVE_RECORD:
            body = {"id": "p147s-4h760", "metadata": {"version": latest}}
            return httpx.Response(200, json=body)
        if url == pnnl.PNNL_GEOJSON_URL:
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/geo+json",
                    "last-modified": "Tue, 31 Mar 2026",
                },
                content=geojson,
            )
        return httpx.Response(404)  # robots.txt and anything else

    return handler


def test_fetch_pnnl_with_version_check(make_test_context: MakeContext) -> None:
    calls: list[str] = []
    ctx = make_test_context(handler=pnnl_handler(calls))
    rows, snap = pnnl.load_pnnl_input(ctx, None, sleep=lambda s: None)
    assert len(rows) == 17
    assert calls == [
        pnnl.MSDLIVE_RECORD + "/versions/latest",
        pnnl.MSDLIVE_RECORD,
        "https://immm-sfa.github.io/robots.txt",
        pnnl.PNNL_GEOJSON_URL,
    ]
    # upstream_version is the version of the file read; the sample's version is not established.
    assert (snap.name, str(snap.url), snap.upstream_version) == (
        "pnnl",
        pnnl.PNNL_GEOJSON_URL,
        None,
    )
    assert snap.last_modified == "Tue, 31 Mar 2026"
    assert ctx.raw_path("pnnl", snap.sha256, "geojson").read_bytes() == GEOJSON.read_bytes()
    assert not any("files" in c or c.endswith((".gpkg", ".csv")) for c in calls)


def test_citation_follows_the_file_read(
    make_test_context: MakeContext,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MSD-LIVE lists a newer version. That says nothing about the web-map file, so neither the
    # receipt nor the records take it.
    ctx = make_test_context(handler=pnnl_handler([], latest="v2026.06.01"))
    _, snap = pnnl.load_pnnl_input(ctx, None, sleep=lambda s: None)
    err = capsys.readouterr().err
    assert snap.upstream_version is None
    assert "MSD-LIVE lists PNNL v2026.06.01" in err and "(not established)" in err
    assert "is not the one checked as v2026.02.09" in err
    s2 = pnnl.pnnl_source(snap)
    assert (str(s2.url), s2.title) == (
        pnnl.PNNL_GEOJSON_URL,
        "IM3 Open Source Data Center Atlas, web map file im3_datacenter_centroids.geojson",
    )
    assert s2.retrieved_at == snap.retrieved_at and s2.license == "ODbL-1.0"

    # A web-map file whose version is established cites that version's DOI, whatever MSD-LIVE's
    # latest version is.
    monkeypatch.setitem(pnnl.WEBMAP_VERSIONS, snap.sha256, "v2026.02.09")
    ctx = make_test_context(handler=pnnl_handler([], latest="v2026.06.01"))
    _, snap = pnnl.load_pnnl_input(ctx, None, sleep=lambda s: None)
    assert "is not the one checked" not in capsys.readouterr().err
    assert snap.upstream_version == "v2026.02.09"
    s2 = pnnl.pnnl_source(snap)
    assert (str(s2.url), s2.title) == (
        "https://doi.org/10.57931/3017294",
        "IM3 Open Source Data Center Atlas v2026.02.09, web map file "
        "im3_datacenter_centroids.geojson",
    )
    # The same file passed with --pnnl is recognised the same way.
    _, local = pnnl.load_pnnl_input(make_test_context(), GEOJSON, sleep=lambda s: None)
    assert local.upstream_version == "v2026.02.09"

    # The MSD-LIVE CSV layout is the v2026.02.09 mirror (07 §4.2 step 3 owner step).
    _, csv_snap = pnnl.load_pnnl_input(make_test_context(), CSV, sleep=lambda s: None)
    s2 = pnnl.pnnl_source(csv_snap)
    assert (csv_snap.upstream_version, str(s2.url), s2.title) == (
        "v2026.02.09",
        "https://doi.org/10.57931/3017294",
        "IM3 Open Source Data Center Atlas v2026.02.09",
    )


def test_established_web_map_file_is_documented() -> None:
    (sha,) = pnnl.WEBMAP_VERSIONS
    assert pnnl.WEBMAP_VERSIONS[sha] == pnnl.PNNL_VERSION
    readme = (FIXTURES / "pnnl" / "README.md").read_text(encoding="utf-8")
    assert sha in readme and "Battelle Memorial Institute" in readme


def test_version_check_failure_is_a_warning(
    make_test_context: MakeContext, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    ctx = make_test_context(handler=pnnl_handler(calls, version_status=503))
    rows, snap = pnnl.load_pnnl_input(ctx, None, sleep=lambda s: None)
    assert len(rows) == 17 and snap.upstream_version is None
    assert "MSD-LIVE version check failed" in capsys.readouterr().err


def test_fetch_failure_raises(make_test_context: MakeContext) -> None:
    ctx = make_test_context()  # every request answers 404
    with pytest.raises(FetchError):
        pnnl.load_pnnl_input(ctx, None, sleep=lambda s: None)
