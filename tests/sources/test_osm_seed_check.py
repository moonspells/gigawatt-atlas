"""Regression tests for what the 2026-10-08 seed check found in the OSM records (n<number> is the
finding). tests/fixtures/osm/overpass-seed-check.json holds the OSM elements of those cases."""

from __future__ import annotations

import argparse
import dataclasses
import json
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from atlas.dissolve import Bounds, OsmObject, dissolve
from atlas.geo.counties import CountyIndex
from atlas.geo.duck import connect
from atlas.geo.places import PlaceIndex
from atlas.schema.record import FacilityRecord
from atlas.sources import pnnl
from atlas.sources.base import ImportContext, ImportResult, InputSnapshot, ReviewItem
from atlas.sources.osm import (
    OVERPASS_QUERY,
    BuildResult,
    OsmImporter,
    build_candidates,
    classify,
    parse_overpass,
    scope_doubt,
)
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SEED_CHECK = FIXTURES / "osm" / "overpass-seed-check.json"
CASES = FIXTURES / "osm" / "overpass-cases.json"
PNNL_SEED_CHECK = FIXTURES / "pnnl" / "centroids-seed-check.geojson"
NO_OVERRIDES = FIXTURES / "osm" / "overrides-empty.json"
OSM_BASE = "2026-10-08T20:47:34Z"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def snapshot(name: str = "overpass") -> InputSnapshot:
    return InputSnapshot(
        name=name,
        url=HttpUrl(
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
            if name == "overpass"
            else pnnl.PNNL_GEOJSON_URL
        ),
        retrieved_at=NOW,
        sha256="0" * 64,
        bytes=1,
        license="ODbL-1.0",
        upstream_version=OSM_BASE if name == "overpass" else None,
    )


def objects(path: Path = SEED_CHECK) -> list[OsmObject]:
    found, skipped = parse_overpass(json.loads(path.read_text(encoding="utf-8")))
    assert skipped == []
    return found


@pytest.fixture
def run(make_test_context: MakeContext) -> ImportResult:
    """The importer on the fixture, as `atlas import osm --input ... --pnnl ...` runs it: with
    the county regions and the PNNL join."""
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    args = parser.parse_args(["--pnnl", str(PNNL_SEED_CHECK), "--overrides", str(NO_OVERRIDES)])
    return OsmImporter().run(make_test_context(input_path=SEED_CHECK), args)


def record(result: ImportResult | BuildResult, ref: str) -> FacilityRecord:
    (found,) = [c.record for c in result.candidates if ref in c.match_values]
    return found


def items(result: ImportResult | BuildResult, kind: str, external_id: str) -> list[ReviewItem]:
    return [i for i in result.review if i.kind == kind and i.external_id == external_id]


def test_the_query_fetches_site_polygons_with_their_outlines() -> None:
    """n44, n48: the query never asked for industrial=data_centre, so 15 PNNL campus rows were
    filed as dropped and campuses split. A second statement now returns those ways and relations
    with `out geom`."""
    first, second = OVERPASS_QUERY.split("out tags bb;")
    assert 'nwr["telecom"="data_center"](area.us);' in first
    for clause in (
        'way["industrial"="data_centre"](area.us);',
        'way["industrial"="data_center"](area.us);',
        'relation["industrial"="data_centre"](area.us);',
        'relation["industrial"="data_center"](area.us);',
    ):
        assert clause in second
    assert second.rstrip().endswith("out geom;")


def test_classify_sites_and_construction_campuses() -> None:
    """n71: a landuse=construction polygon tagged construction=data_center was a building, so the
    halls built on it stayed apart ("AWS IAD-500 and IAD-501")."""
    assert classify("way", {"construction": "data_center", "landuse": "construction"}) == "campus"
    assert classify("way", {"construction:telecom": "data_center"}) == "campus"
    assert classify("way", {"industrial": "data_centre", "landuse": "industrial"}) == "site"
    assert classify("relation", {"industrial": "data_center"}) == "site"
    assert classify("way", {"industrial": "data_centre", "telecom": "data_center"}) == "campus"
    assert classify("way", {"industrial": "data_center", "building": "industrial"}) == "building"
    assert classify("node", {"industrial": "data_centre"}) == "point"


def test_parse_reads_outlines_and_merges_the_second_copy() -> None:
    by_ref = {o.ref: o for o in objects()}
    site = by_ref["way/1377227157"]
    assert site.kind == "site" and site.outline is not None
    (ring,) = site.outline
    assert ring[0] == ring[-1] and len(ring) == 15
    # An element that comes in both statements is read once, with the outline of the copy that
    # has one.
    element = json.loads(SEED_CHECK.read_text(encoding="utf-8"))["elements"]
    copy = next(e for e in element if e["id"] == 1377227157)
    bb = {k: v for k, v in copy.items() if k != "geometry"}
    (merged,), _ = parse_overpass({"elements": [bb, copy]})
    assert merged.outline == site.outline
    relation = {
        "type": "relation",
        "id": 9,
        "bounds": {"minlat": 0.0, "minlon": 0.0, "maxlat": 1.0, "maxlon": 1.0},
        "members": [
            {
                "type": "way",
                "ref": 1,
                "role": "outer",
                "geometry": [{"lat": 0, "lon": 0}, {"lat": 0, "lon": 1}, {"lat": 1, "lon": 1}],
            },
            {
                "type": "way",
                "ref": 2,
                "role": "outer",
                "geometry": [{"lat": 1, "lon": 1}, {"lat": 0, "lon": 0}],
            },
            {"type": "node", "ref": 3, "role": "label", "lat": 0.5, "lon": 0.5},
        ],
        "tags": {"industrial": "data_centre"},
    }
    (rel,), _ = parse_overpass({"elements": [relation]})
    # A triangle in two member ways: (0.2, 0.8) is inside it, (0.8, 0.2) only inside its box.
    assert rel.kind == "site" and rel.outline is not None and len(rel.outline) == 2
    assert rel.covers(0.2, 0.8) and not rel.covers(0.8, 0.2)


def test_a_site_polygon_holds_its_buildings(run: ImportResult) -> None:
    """n48: the four unnamed buildings inside "Microsoft Bison Business Park Data Center" were
    four "Data center (Laramie County, WY)" records. They join the site and take its name and
    operator."""
    r = record(run, "way/1377227157")
    assert r.external_ids["osm"] == [
        "way/1377227154",
        "way/1377227157",
        "way/1485867694",
        "way/1485867695",
        "way/1485867696",
    ]
    assert r.canonical_name == "Microsoft Bison Business Park Data Center (Laramie County, WY)"
    assert [o.name for o in r.parties.operator] == ["Microsoft"]
    assert r.location.geometry_ref == "osm:way/1377227157"
    assert [b.ref for b in r.buildings] == [
        "osm:way/1377227154",
        "osm:way/1485867694",
        "osm:way/1485867695",
        "osm:way/1485867696",
    ]


def test_a_site_polygon_alone_is_held(counties: CountyIndex) -> None:
    """A site polygon with no data center object inside makes no record: OSM says a site is
    there, not whether it is built, and no status is invented (07 §2.3)."""
    site = next(o for o in objects() if o.ref == "way/1377227157")
    result = build_candidates(dissolve([site]), counties=counties, now=NOW, snapshot=snapshot())
    assert result.candidates == [] and result.metrics["held"] == 1
    (item,) = result.review
    assert (item.kind, item.external_id) == ("unknown_status", "way/1377227157")
    assert "does not say whether it is built" in item.reason
    assert item.data["name"] == "Microsoft Bison Business Park Data Center"


def test_an_industrial_data_centre_building_is_a_data_center(counties: CountyIndex) -> None:
    """Seed check (OSM group): "IBM Quantum Data Center", way 195803647, is tagged only
    building=industrial + industrial=data_center, so it had no status and made no record. With a
    building tag, industrial=data_centre counts like building=data_center (lifecycle tags first);
    its status is an observation that dates nothing."""
    lat, lon = 41.2105, -73.8045  # Westchester County, NY
    b = Bounds(lat - 0.0005, lon - 0.0005, lat + 0.0005, lon + 0.0005)
    tags = {"building": "industrial", "industrial": "data_center", "name": "Example Quantum DC"}
    ibm = OsmObject("way/195803647", lat, lon, b, tags, classify("way", tags))
    result = build_candidates(dissolve([ibm]), counties=counties, now=NOW, snapshot=snapshot())
    (c,) = result.candidates
    r = c.record
    assert r.status == "operating" and r.dates == {}
    (event,) = r.status_history
    assert (event.event, event.status) == ("other", "operating")
    assert event.note is not None and "industrial=data_center" in event.note
    # The same tags under construction, or planned, read the lifecycle tag first.
    being_built = OsmObject(
        "way/2", lat, lon, b, {**tags, "building": "construction"}, classify("way", tags)
    )
    result = build_candidates(
        dissolve([being_built]), counties=counties, now=NOW, snapshot=snapshot()
    )
    assert [c.record.status for c in result.candidates] == ["under_construction"]


def test_a_construction_site_holds_its_halls(run: ImportResult) -> None:
    """n71: "AWS IAD-500 and IAD-501" (landuse=construction, construction=data_center) and the
    operating IAD-500 inside it were separate records with conflicting statuses."""
    r = record(run, "way/1319699416")
    assert "way/1426663253" in r.external_ids["osm"]
    assert r.canonical_name == "AWS IAD-500 and IAD-501 (Fairfax County, VA)"
    assert r.status == "operating" and {p.phase_id for p in r.phases} == {
        "osm-operating",
        "osm-under_construction",
    }


def test_clusters_stay_in_one_county(run: ImportResult) -> None:
    """n10: xAI's building in Southaven, DeSoto County, MS, was joined into a Shelby County, TN,
    record by rule 2 (same operator within 300 m). The county line now keeps them apart, and
    (second seed check n11) the smaller part makes no record: one campus counts once until a
    reviewer decides."""
    memphis = record(run, "way/1386926534")
    assert memphis.external_ids["osm"] == ["way/1386926534"]
    assert (memphis.location.county_fips, memphis.location.state_abbr) == ("47157", "TN")
    assert not any("way/1077131079" in c.match_values for c in run.candidates)
    (held,) = items(run, "possible_duplicate", "way/1077131079")
    assert held.data["other"] == "way/1386926534"
    assert "DeSoto County" in held.reason and "Shelby County" in held.reason


def test_same_name_and_same_address_join(run: ImportResult) -> None:
    """n13: QTS Data Center - Hillsboro 3's node has no operator and stayed apart from its two
    polygons. n61: an unnamed way at 8100 Boone Boulevard (and one at 2220 De La Cruz Boulevard)
    was a record next to the operator's object at the same address."""
    qts = record(run, "node/11721960464")
    assert qts.external_ids["osm"] == ["node/11721960464", "way/1465196735", "way/1465196736"]
    boone = record(run, "node/7985753364")
    assert boone.external_ids["osm"] == ["node/7985753364", "way/156468497"]
    sc1 = record(run, "way/358455179")
    assert sc1.external_ids["osm"] == ["way/358455179", "way/1075445245"]
    assert [o.name for o in sc1.parties.operator] == ["Digital Realty"]


def test_operator_conflicts_are_flagged(run: ImportResult) -> None:
    """n54, n72: a node inside another operator's building (CoreSite LA2 in the USPO Terminal
    Annex) and AWS buildings inside a Microsoft polygon that has no building of its own were kept
    apart with no review item. Since the second seed check (n0, n3) the post office's operator is
    not the data center's, so LA2 joins the building, and the polygon, which its buildings'
    addresses contradict, is held with a possible_duplicate item instead of a record."""
    la2 = record(run, "node/13042311881")
    assert la2.external_ids["osm"] == ["node/13042311881", "way/30666790"]
    assert items(run, "possible_duplicate", "node/13042311881") == []
    (quail,) = items(run, "possible_duplicate", "way/897226569")
    assert quail.data["other"] == "way/1301654223"
    assert quail.data["osm"] == [
        "way/897226574",
        "way/897226575",
        "way/1301654223",
        "way/1301654224",
    ]
    assert not any("way/897226569" in c.match_values for c in run.candidates)


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("way/253670255", "is tagged as a power plant"),  # n49 Greenidge (generator:source)
        ("way/1334234454", "is named as a cryptocurrency mine ('Cryptomine')"),  # n49 Nautilus
        ("node/2607827436", "a room or a non-compute use ('Computer Room')"),  # n49 SDSU
        ("way/758580536", "a room or a non-compute use ('Vital Records')"),  # n58 tape vault
        ("node/10938672218", "a point with a business name and no operator"),  # n63 K-Motion
        ("way/1013396352", "named only as a telephone company ('CenturyLink')"),  # n63
        ("way/1552427895", "is an outbuilding (building=shed)"),  # n70 The Putney School
        ("way/1020721870", "a local government's building ('City of Searcy')"),  # n70
    ],
)
def test_doubtful_objects_are_held_out_of_scope(run: ImportResult, ref: str, why: str) -> None:
    """n49, n58, n63, n70: crypto mines, an in-building computer room, a tape vault, offices and
    a telecom shed tagged telecom=data_center were published as operating data centers. They are
    kept with scope out_of_scope and an out_of_scope item, so a reviewer decides (07 §2.2)."""
    r = record(run, ref)
    assert (r.scope, r.purpose) == ("out_of_scope", "unknown")
    (item,) = items(run, "out_of_scope", r.external_ids["osm"][0])
    assert why in item.reason and "until a reviewer decides" in item.reason


def test_data_centers_the_screen_keeps(run: ImportResult) -> None:
    # Under 200 m², but its operator says data center; a node that names an operator known
    # elsewhere in the input (TierPoint, way 465042594).
    for ref in ("way/967131562", "node/9721703993"):
        assert record(run, ref).scope == "in_scope", ref
    tiny = OsmObject("way/1", 39.0, -77.0, Bounds(39.0, -77.0, 39.0001, -77.0001), {}, "building")
    assert scope_doubt(dissolve([tiny])[0], frozenset()) is not None


def test_a_planned_lot_without_a_name_is_a_lead(run: ImportResult) -> None:
    """n80: OSM proposed=yes on an anonymous lot (Manchester Township, NJ, where no application
    was filed) was published as proposed, 07 §2.3's pending filing. It makes no record; a named
    planned site is announced."""
    assert not [c for c in run.candidates if "way/1549253250" in c.match_values]
    (item,) = items(run, "unverified_upstream", "way/1549253250")
    assert "no name and no operator" in item.reason
    assert record(run, "node/11721960464").status == "announced"


def test_approximate_positions_lower_the_precision(run: ImportResult) -> None:
    """n64: note=location is approximate, and the record said footprint."""
    r = record(run, "way/1544360250")
    assert r.location.precision == "site"
    (item,) = items(run, "unverified_upstream", "way/1544360250")
    assert "'location is approximate'" in item.reason


def test_the_street_is_the_representatives(run: ImportResult) -> None:
    """n6: QTS Manassas DC5 (no address) was published at DC1's 9400 Godwin Drive, the most common
    member street. A member's street is used only when every member that has one agrees."""
    r = record(run, "way/1090837713")
    # Manassas is the postal city (addr:city); the point lies in Innovation CDP (07 §6.5).
    assert r.canonical_name == "QTS Manassas DC5 (Innovation, VA)"
    assert (r.location.street, r.location.city) == (None, "Innovation")
    flexential = record(run, "way/392324240")
    assert flexential.location.street == "2775 Northwoods Parkway"


def test_the_name_gives_the_county_not_the_postal_city(run: ImportResult) -> None:
    """n57: addr:city=Norcross is the postal city; the point lies in Peachtree Corners. Without a
    Census place that holds the point (the sample has none here), the name gives the county, which
    contains it, and location.city is empty: addr:city is never published as the city."""
    r = record(run, "way/392324240")
    assert r.canonical_name == "Flexential Atlanta - Norcross (Gwinnett County, GA)"
    assert r.location.city is None


def places_around(tmp_path: Path, lat: float, lon: float, name: str, state: str) -> PlaceIndex:
    """A place file with one square place 0.02 degrees across, centred on the point."""
    con = connect()
    con.execute(
        "CREATE TABLE t (GEOID VARCHAR, NAME VARCHAR, NAMELSAD VARCHAR, LSAD VARCHAR,"
        " STUSPS VARCHAR, geom GEOMETRY)"
    )
    box = (
        f"POLYGON(({lon - 0.01} {lat - 0.01}, {lon + 0.01} {lat - 0.01}, {lon + 0.01} {lat + 0.01},"
        f" {lon - 0.01} {lat + 0.01}, {lon - 0.01} {lat - 0.01}))"
    )
    con.execute(
        "INSERT INTO t VALUES ('1399999', ?, ?, '25', ?, ST_GeomFromText(?))",
        [name, f"{name} city", state, box],
    )
    con.execute(f"COPY t TO '{tmp_path / 'one.shp'}' WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile')")
    con.close()
    out = tmp_path / "one.zip"
    with zipfile.ZipFile(out, "w") as z:
        for ext in ("shp", "shx", "dbf"):
            z.write(tmp_path / f"one.{ext}", f"one.{ext}")
    return PlaceIndex.load(out, verify_sha256=False)


def test_the_city_is_the_census_place_that_contains_the_point(
    run: ImportResult, make_test_context: MakeContext, tmp_path: Path
) -> None:
    """n57 (with the place polygons): Flexential's point lies in Peachtree Corners, so that is its
    city and the name gives it; the postal city Norcross is not kept. A place in another state is
    not the record's."""
    loc = record(run, "way/392324240").location
    assert loc.lat is not None and loc.lon is not None
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    args = parser.parse_args(["--pnnl", str(PNNL_SEED_CHECK), "--overrides", str(NO_OVERRIDES)])
    peachtree = places_around(tmp_path, loc.lat, loc.lon, "Peachtree Corners", "GA")
    ctx = make_test_context(input_path=SEED_CHECK, places=peachtree)
    r = record(OsmImporter().run(ctx, args), "way/392324240")
    assert r.location.city == "Peachtree Corners"
    assert r.canonical_name == "Flexential Atlanta - Norcross (Peachtree Corners, GA)"
    (tmp_path / "x").mkdir()
    elsewhere = places_around(tmp_path / "x", loc.lat, loc.lon, "Somewhere", "AL")
    ctx = make_test_context(input_path=SEED_CHECK, places=elsewhere)
    r = record(OsmImporter().run(ctx, args), "way/392324240")
    assert (r.location.city, r.canonical_name) == (
        None,
        "Flexential Atlanta - Norcross (Gwinnett County, GA)",
    )


def test_the_pnnl_rows_go_by_position_and_name(run: ImportResult) -> None:
    """n4: the PNNL row "QTS Manassas DC1" lies on DC2's center, but its stale name sent it to DC1
    and filed a misleading possible_duplicate. n45: Fiberhub LAS1's node moved 119 m after PNNL
    took its position, and the row was filed as dropped."""
    qts = record(run, "way/1090837713")
    assert qts.external_ids["pnnl_im3"] == [
        "building@-77.510797,38.753195",
        "building@-77.512523,38.752551",
        "building@-77.513111,38.752517",
        "building@-77.517447,38.754693",
    ]
    assert not [i for i in run.review if i.source == "pnnl" and i.kind != "unmatched"]
    assert record(run, "node/13311012216").external_ids["pnnl_im3"] == [
        "point@-115.139883,36.065941"
    ]
    assert run.metrics["pnnl_matched_by_name"] == 1
    # n41, n46: the reason says what was tested and names the nearest element.
    (gone,) = [i for i in run.review if i.kind == "unmatched"]
    assert gone.external_id == "building@-74.443095,40.494903"
    assert gone.reason.startswith(
        "no OSM data center element this import read takes the PNNL building row: no bounding "
        "box (+30 m) holds its point, no center is within 50 m and no element of its name is "
        "within 250 m; the nearest is way/758580536,"
    )
    assert gone.data["nearest_osm"] == "way/758580536"


def test_every_record_validates(run: ImportResult, counties: CountyIndex) -> None:
    for c in run.candidates:
        r = c.record
        assert validate_record(r, counties=counties, today=NOW.date()) == [], c.match_values
        assert r.dates == {}, c.match_values  # n0: nothing here dates a status


def test_a_footprint_drawn_twice_is_held(counties: CountyIndex) -> None:
    """n2: an unnamed way without an operator on the Apple Data Center's footprint in Mesa
    (way 567575425 on way 300974499) was a second record."""
    result = build_candidates(
        dissolve(objects(CASES)), counties=counties, now=NOW, snapshot=snapshot()
    )
    assert not [c for c in result.candidates if "way/567575425" in c.match_values]
    (item,) = items(result, "possible_duplicate", "way/567575425")
    assert item.data["other"] == "way/300974499"


def test_records_of_one_name_close_together_are_flagged(counties: CountyIndex) -> None:
    """n62, n79: unnamed halls more than 50 m apart in one site stay separate records with one
    generated name. They raise one possible_duplicate item per group."""

    def hall(ref: str, east: float) -> OsmObject:
        lat, lon = 32.40, -99.80 + east
        tags = {"building": "data_center", "telecom": "data_center"}
        return OsmObject(
            ref, lat, lon, Bounds(lat - 2e-4, lon - 2e-4, lat + 2e-4, lon + 2e-4), tags, "building"
        )

    halls = [hall("way/1", 0), hall("way/2", 0.002), hall("way/3", 0.004), hall("way/4", 0.1)]
    result = build_candidates(dissolve(halls), counties=counties, now=NOW, snapshot=snapshot())
    assert len(result.candidates) == 4
    (item,) = [i for i in result.review if i.kind == "possible_duplicate"]
    assert item.data["representatives"] == ["way/1", "way/2", "way/3"]
    assert item.reason.startswith("3 records are called 'Data center (Taylor County, TX)'")


def test_a_mine_on_a_data_center_site_does_not_hold_the_site(counties: CountyIndex) -> None:
    """A crypto mine on a campus that also has a data center building (the Susquehanna site holds
    Nautilus Cryptomine and Amazon's buildings) keeps the cluster in scope; a site polygon tagged
    as a mine, or a cluster whose every named member is one, is held."""
    lat, lon = 41.08, -76.15
    site = OsmObject(
        "way/1",
        lat,
        lon,
        Bounds(lat - 0.005, lon - 0.005, lat + 0.005, lon + 0.005),
        {"industrial": "data_centre", "landuse": "industrial"},
        "site",
    )

    def hall(ref: str, dlat: float, **tags: str) -> OsmObject:
        b = Bounds(lat + dlat - 3e-4, lon - 3e-4, lat + dlat + 3e-4, lon + 3e-4)
        return OsmObject(ref, *b.center, b, {"telecom": "data_center", **tags}, "building")

    mine = hall("way/2", 0.002, name="Nautilus Cryptomine")
    aws = hall("way/3", -0.002, name="Amazon PHL", operator="Amazon Web Services")
    (both,) = dissolve([site, mine, aws])
    assert scope_doubt(both, frozenset()) is None
    (alone,) = dissolve([mine])
    assert (
        scope_doubt(alone, frozenset()) == "way/2 is named as a cryptocurrency mine ('Cryptomine')"
    )
    crypto_site = dataclasses.replace(site, tags={**site.tags, "data_centre": "crypto"})
    (held,) = dissolve([crypto_site, aws])
    assert scope_doubt(held, frozenset()) == "way/1 is tagged as a cryptocurrency mine"
