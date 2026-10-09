"""Regression tests for what the second seed check of 2026-10-09 found in the OSM records (n<number>
is the finding in the check's bug list), and for config/overrides/osm.json. The elements are in
tests/fixtures/osm/overpass-fix4.json, copied from the seed's Overpass response; the PNNL rows in
tests/fixtures/pnnl/centroids-fix4.geojson."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from atlas.geo.counties import CountyIndex
from atlas.net import FetchError
from atlas.schema.record import FacilityRecord
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.sources.osm import (
    OVERPASS_QUERY,
    OVERRIDES_PATH,
    OsmImporter,
    apply_overrides,
    keep_containers,
    load_overrides,
    parse_overpass,
)
from atlas.text import find_personal_data
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIX4 = FIXTURES / "osm" / "overpass-fix4.json"
PNNL_FIX4 = FIXTURES / "pnnl" / "centroids-fix4.geojson"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / OVERRIDES_PATH
NO_OVERRIDES = FIXTURES / "osm" / "overrides-empty.json"


def importer_args(overrides: Path = REPO_OVERRIDES) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    return parser.parse_args(["--pnnl", str(PNNL_FIX4), "--overrides", str(overrides)])


@pytest.fixture
def run(make_test_context: MakeContext) -> ImportResult:
    """The importer on the fixture with the committed overrides, as `atlas import osm --input
    ... --pnnl ...` runs it (every element config/overrides/osm.json names is in the fixture)."""
    return OsmImporter().run(make_test_context(input_path=FIX4), importer_args())


def record(result: ImportResult, ref: str) -> FacilityRecord:
    (found,) = [c.record for c in result.candidates if ref in c.match_values]
    return found


def no_record(result: ImportResult, ref: str) -> bool:
    return not any(ref in c.match_values for c in result.candidates)


def items(result: ImportResult, kind: str, external_id: str) -> list[ReviewItem]:
    return [i for i in result.review if i.kind == kind and i.external_id == external_id]


def test_every_record_validates(run: ImportResult, counties: CountyIndex, today: date) -> None:
    for c in run.candidates:
        assert validate_record(c.record, counties=counties, today=today) == [], c.match_values


# ---------------------------------------------------------------------------- one site, one record


def test_a_post_office_does_not_name_the_data_center(run: ImportResult) -> None:
    """n0: the USPO Terminal Annex (amenity=post_office, operator USPS) was published as 'United
    States Postal Service USPO Terminal Annex' next to CoreSite LA2's node inside it."""
    la2 = record(run, "node/13042311881")
    assert la2.external_ids["osm"] == ["node/13042311881", "way/30666790"]
    assert la2.canonical_name == "CoreSite - LA2 (Los Angeles County, CA)"
    assert [o.name for o in la2.parties.operator] == ["CoreSite"]
    assert la2.parties.owner == []
    assert [a.name for a in la2.aliases] == ["USPO Terminal Annex"]
    assert items(run, "possible_duplicate", "node/13042311881") == []


def test_one_buildings_owner_is_not_the_campus_owner(run: ImportResult) -> None:
    """n1: only Equinix DC10 of six buildings carries owner=Digital Realty (sold in 2021), and
    the campus published it as its owner."""
    dc10 = record(run, "way/460050155")
    assert len(dc10.external_ids["osm"]) == 6
    assert dc10.parties.owner == []
    # Every member names the owner: the campus gives it (Digital Realty on the Burbank building).
    burbank = record(run, "way/460212563")
    assert [o.name for o in burbank.parties.owner] == ["Digital Realty"]


def test_sibling_buildings_are_one_record(run: ImportResult) -> None:
    """n2, n13, n36: KOMO Plaza East and West; PowerHouse Pacific Buildings 1 to 3; EAT12 to
    EAT14."""
    assert record(run, "way/417682780").external_ids["osm"] == ["way/417682780", "way/417682781"]
    powerhouse = record(run, "way/1534356805")
    assert powerhouse.external_ids["osm"] == ["way/1534356804", "way/1534356805", "way/1544360250"]
    assert [o.name for o in powerhouse.parties.operator] == ["PowerHouse Data Centers"]
    eat = record(run, "way/1173822721")
    assert eat.external_ids["osm"] == ["way/1173822721", "way/1173822722", "way/1173822723"]


def test_an_operator_named_by_the_name_joins_its_campus(run: ImportResult) -> None:
    """n33: 'Google Leesburg Building 3' and 'CyrusOne San Antonio IV' have no operator tag."""
    google = record(run, "way/1460776174")
    assert "way/706726088" in google.external_ids["osm"]
    assert [o.name for o in google.parties.operator] == ["Google"]
    cyrusone = record(run, "way/1121592786")
    assert cyrusone.external_ids["osm"] == ["way/509611438", "way/509611439", "way/1121592786"]


def test_compass_red_oak_is_one_record(run: ImportResult) -> None:
    """n6, n8: the eleven 'Compass Data Center' buildings at Red Oak were nine records: no
    operator tag, 54 m apart, and the name is not specific, since Compass Datacenters operates
    Compass IAD IE (in the fixture). The name implies Compass Datacenters for the joins; no
    operator is published from a name."""
    compass = record(run, "way/1426525212")
    expected = [f"way/{i}" for i in [850965775, *range(1426525212, 1426525222)]]
    assert compass.external_ids["osm"] == expected
    assert compass.parties.operator == []
    assert compass.canonical_name.startswith("Compass Data Center (")


def test_one_name_and_one_operator_within_500_m_are_one_record(run: ImportResult) -> None:
    """n8, n4, n6: QTS Phoenix II (345 m), QTS Fayetteville's pair (314 m) and Google Council
    Bluffs' four expansion halls (478 m) were separate records of one campus."""
    assert record(run, "way/1132632705").external_ids["osm"] == ["way/1132632705", "way/1315636714"]
    assert record(run, "way/1510517635").external_ids["osm"] == ["way/1510517635", "way/1510517636"]
    assert len(record(run, "way/1565614265").external_ids["osm"]) == 18


def test_large_unnamed_halls_are_one_record(run: ImportResult) -> None:
    """n6, n13: Stream San Antonio III's three halls, the Prince William site's two buildings and
    QTS Atlanta DC3 and DC4 have no name and no operator."""
    stream = ["way/1432637060", "way/1432637061", "way/1432637062"]
    assert record(run, stream[0]).external_ids["osm"] == stream
    assert record(run, "way/1509985717").external_ids["osm"] == ["way/1509985717", "way/1561790221"]
    assert record(run, "way/1566676867").external_ids["osm"] == ["way/1566676867", "way/1566676868"]


def test_a_development_mapped_hall_by_hall_publishes_once(run: ImportResult) -> None:
    """n6, n13: the 40 halls of the Lancium Clean Campus (no site polygon in the input) were
    eight records 'Data center (Taylor County, TX)'. The largest stays; the others are held."""
    halls = [f"way/{i}" for i in [*range(1472056705, 1472056715), *range(1530966366, 1530966396)]]
    published = {record(run, h).id for h in halls if not no_record(run, h)}
    assert len(published) == 1
    kept = next(c for c in run.candidates if halls[0] in c.match_values)
    assert len(kept.match_values) == 10
    held = [i for i in run.review if i.kind == "possible_duplicate" and "hall by hall" in i.reason]
    assert len(held) == 6
    assert kept.record.location.geometry_ref == "osm:way/1472056706"  # the largest hall's box
    assert all(i.data["other"] == "way/1472056706" for i in held)
    assert sum(len(refs) for i in held if isinstance(refs := i.data["refs"], list)) == 30


def test_a_polygon_its_buildings_contradict_is_held(run: ImportResult) -> None:
    """n3: Microsoft's Quail Ridge Lane polygon has no building of its own; the four AWS
    buildings inside it are at the addresses it lists. It made a 'Microsoft' record."""
    assert no_record(run, "way/897226569")
    (held,) = items(run, "possible_duplicate", "way/897226569")
    assert held.data["other"] == "way/1301654223"
    assert "contradicted" in held.reason
    aws = record(run, "way/1301654223")
    assert len(aws.external_ids["osm"]) == 4
    assert aws.location.street == "24214 Quail Ridge Lane"


def test_a_site_named_two_ways_is_held(run: ImportResult) -> None:
    """n9: STACK's NVA04 polygon lists the addresses of the AWS-tagged IAD-285 and IAD-286
    inside it; the site was published twice, once under each operator."""
    assert no_record(run, "way/1344901566") and no_record(run, "way/1344901568")
    (stack,) = items(run, "conflict", "way/1344901566")
    (aws,) = items(run, "conflict", "way/1344901568")
    assert stack.data["other"] == "way/1344901568" and aws.data["other"] == "way/1344901566"


def test_a_contractor_and_a_typo_do_not_split_a_site(run: ImportResult) -> None:
    """n10, n14: HITT, the builder, was the operator of 'HITT data center' over Digital Realty
    IAD51; 'CyrysOne', a typo, kept the CyrusOne halls out of their campus polygon and named it
    'CyrysOne CyrusOne Data Center Campus'."""
    iad51 = record(run, "way/1561771300")
    assert iad51.external_ids["osm"] == ["way/1194169753", "way/1561771300"]
    assert iad51.canonical_name == "Digital Realty IAD51 (Prince William County, VA)"
    assert [o.name for o in iad51.parties.operator] == ["Digital Realty"]
    (hitt,) = [i for i in items(run, "unverified_upstream", "way/1194169753") if "HITT" in i.reason]
    assert "general contractor" in hitt.reason
    assert record(run, "way/996899486").external_ids["osm"] == [
        "way/996899486",
        "way/996899487",
        "way/1227612684",
    ]
    campus = record(run, "way/1556736879")
    assert campus.external_ids["osm"] == [
        "way/1556736877",
        "way/1556736878",
        "way/1556736879",
        "way/1556736880",
    ]
    assert campus.canonical_name == "CyrusOne Data Center Campus (Freestone County, TX)"
    assert [o.name for o in campus.parties.operator] == ["CyrusOne"]
    (typo,) = [
        i for i in items(run, "unverified_upstream", "way/1556736879") if "letter" in i.reason
    ]
    assert "CyrysOne" in typo.reason


def test_a_county_line_split_holds_the_smaller_part(run: ImportResult) -> None:
    """n11: Digital Realty IAD53 (Manassas city) is 155 m from IAD51 (Prince William County),
    the same operator; the county rule made it a record of its own."""
    assert no_record(run, "way/1269259655")
    (held,) = items(run, "possible_duplicate", "way/1269259655")
    assert held.data["other"] == "way/1194169753"
    assert "Manassas city" in held.reason and "Prince William County" in held.reason


def test_a_site_outline_holds_its_campus_across_a_county_line(run: ImportResult) -> None:
    """n40: Microsoft TRP3 (Medina County) lies in the Texas Research Park Campus outline (Bexar
    County); Google NBY-4 to NBY-6 (Franklin County) in Google's New Albany site (Licking
    County). Each was a record of its own."""
    trp = record(run, "way/1010260675")
    assert "way/521300038" in trp.external_ids["osm"]
    assert trp.location.county_fips == "48029"
    (line,) = items(run, "county_mismatch", "way/521300038")
    assert line.data["osm"] == ["way/1010260675"] and "Medina County" in line.reason
    nby = record(run, "way/1252196812")
    assert {"way/844391357", "way/1386016632", "way/1386016633"} <= set(nby.external_ids["osm"])
    assert nby.location.county_fips == "39089"


# ---------------------------------------------------------------------------- fields


def test_a_list_of_house_numbers_is_not_a_street_address(run: ImportResult) -> None:
    """n5, n12, n16, n41: '3825;3855 Northeast Aloclek Drive' and 'FM56;County Road 3610A' were
    published as streets."""
    assert record(run, "way/328702750").location.street == "Northeast Aloclek Drive"
    assert record(run, "way/1556736805").location.street is None


def test_no_acreage_from_an_approximate_or_another_boundary(run: ImportResult) -> None:
    """n7: Meta Cheyenne's site is 'location and boundary very approximate', and PNNL's 260.6
    acres measure an older version of it; Amazon New Albany's Jug and Beach Road row (35.0 acres)
    describes another polygon than the 113.7-acre outline read."""
    meta = record(run, "way/1455907959")
    assert meta.site.acreage is None
    (approx,) = [i for i in run.review if i.source == "pnnl" and "approximate" in i.reason]
    assert approx.data["osm"] == "way/1455907959" and "260.6 acres" in approx.reason
    assert record(run, "way/1281982572").site.acreage is None
    (other,) = [i for i in run.review if i.source == "pnnl" and "another version" in i.reason]
    assert other.data["osm"] == "way/1281982572"


# ---------------------------------------------------------------------------- scope screen


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("way/1092757323", "is tagged resource=cryptocurrency, a cryptocurrency mine"),  # n35
        ("node/10567817971", "a room or a non-compute use ('Lab')"),  # n38 Duke, a library lab
        ("way/836947535", "is tagged healthcare:speciality=neurology, another primary use"),  # n39
        ("way/42000820", "is tagged amenity=university, another primary use"),  # 'Old Main'
    ],
)
def test_doubtful_objects_are_held_out_of_scope(run: ImportResult, ref: str, why: str) -> None:
    r = record(run, ref)
    assert r.scope == "out_of_scope"
    (item,) = items(run, "out_of_scope", ref)
    assert why in item.reason and "until a reviewer decides" in item.reason


# ---------------------------------------------------------------------------- the query


def test_the_query_fetches_outlines_and_containers() -> None:
    """n3, n6, n9: the campus polygons came without outlines, and the construction or industrial
    polygons around a campus (the Lancium Clean Campus, Stream San Antonio III, Compass Red Oak)
    never came at all."""
    first, rest = OVERPASS_QUERY.split(".dc out tags bb;")
    outlines, containers, tail = rest.split("out geom;")
    assert first.rstrip().endswith(")->.dc;")
    for clause in ('way.dc[!"building"];', 'relation.dc["building"="no"];'):
        assert clause in outlines
    assert 'way["industrial"="data_centre"](area.us);' in outlines
    assert "(node(w.dc:1); node.dc;)->.pts;" in containers and "is_in->.inside;" in containers
    landuse = '["landuse"~"^(construction|industrial|commercial)$"];'
    assert f"way(pivot.inside){landuse}" in containers
    assert f"relation(pivot.inside){landuse}" in containers
    assert tail.strip() == ""


def test_containers_need_two_objects_and_a_data_center_name() -> None:
    lancium = {"type": "way", "id": 1, "tags": {"landuse": "construction", "name": "Lancium"}}
    estate = {"type": "way", "id": 2, "tags": {"landuse": "industrial", "name": "Estate Park"}}
    compass = {
        "type": "way",
        "id": 3,
        "tags": {"landuse": "industrial", "name": "Compass Data Center"},
    }
    lone = {"type": "way", "id": 4, "tags": {"landuse": "construction"}}
    big = {"minlat": 0.0, "minlon": 0.0, "maxlat": 0.01, "maxlon": 0.01}
    small = {"minlat": 0.02, "minlon": 0.02, "maxlat": 0.03, "maxlon": 0.03}
    halls = [
        {"type": "way", "id": 10 + i, "bounds": b, "tags": {"telecom": "data_center"}}
        for i, b in enumerate(
            [
                {"minlat": 0.001, "minlon": 0.001, "maxlat": 0.002, "maxlon": 0.002},
                {"minlat": 0.005, "minlon": 0.005, "maxlat": 0.006, "maxlon": 0.006},
            ]
        )
    ]
    elements = [
        {**lancium, "bounds": big},
        {**estate, "bounds": big},
        {**compass, "bounds": big},
        {**lone, "bounds": small},
        *halls,
    ]
    objects, _ = parse_overpass({"elements": elements})
    assert {o.ref: o.container for o in objects if o.kind == "site"} == {
        "way/1": True,
        "way/2": True,
        "way/3": True,
        "way/4": True,
    }
    kept = {o.ref for o in keep_containers(objects)}
    assert kept == {"way/1", "way/3", "way/10", "way/11"}


# ---------------------------------------------------------------------------- overrides


def test_the_committed_overrides_correct_stale_tags(run: ImportResult) -> None:
    """Record errors of 2026-10-09 that are OpenStreetMap's own (tags out of date), corrected by
    cited entries in config/overrides/osm.json."""
    canopy = record(run, "node/10780794741")
    assert canopy.canonical_name == "Data Canopy data center (San Francisco County, CA)"
    (op,) = canopy.parties.operator
    assert (op.name, op.source_ids) == ("Data Canopy", ["s3"])
    s3 = next(s for s in canopy.sources if s.id == "s3")
    assert (s3.publisher, s3.quote_match, s3.supports) == ("Baxtel", "human", ["/canonical_name"])
    assert s3.quote is not None and "Ntirety" in s3.quote
    s1 = canopy.sources[0]
    assert "/canonical_name" not in s1.supports and "/parties" not in s1.supports
    # Intergate Building 4 is Sabey's: it joins the Sabey campus.
    sabey = record(run, "way/293211687")
    assert "way/293211686" in sabey.external_ids["osm"]
    ((sabey_name, sabey_sids),) = [(o.name, o.source_ids) for o in sabey.parties.operator]
    # The entry's source for Building 4, the members' own elements for the others (n12).
    assert sabey_name == "Sabey Data Centers" and sabey_sids[:2] == ["s1", "s3"]
    osm_ids = {s.id for s in sabey.sources if s.url.host == "www.openstreetmap.org"}
    assert set(sabey_sids) - {"s3"} <= osm_ids
    # A tenant that left: the name is removed, not replaced.
    vita = record(run, "way/439365340")
    assert vita.canonical_name == "Data center (Chesterfield County, VA)"
    assert vita.aliases == []
    burbank = record(run, "way/460212563")
    assert burbank.canonical_name == "Centersquare data center (Los Angeles County, CA)"
    gi = record(run, "way/635022480")
    assert gi.canonical_name == "GI Partners data center (Fulton County, GA)"
    assert run.metrics["overrides"] == 5


def write(tmp_path: Path, entries: Mapping[str, object]) -> Path:
    path = tmp_path / "osm.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


ENTRY: dict[str, object] = {
    "reason": "the source says so",
    "source_url": "https://example.com/page",
    "publisher": "Example",
    "quote": "A sentence that states the correction.",
    "retrieved_at": "2026-10-09T12:00:00Z",
    "reviewed_at": "2026-10-09",
}


def test_an_override_drops_an_element_from_scope(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    path = write(tmp_path, {"way/1569999999": {**ENTRY, "drop": True}} | {})
    with pytest.raises(FetchError, match=r"does not have: way/1569999999"):
        OsmImporter().run(make_test_context(input_path=FIX4), importer_args(path))
    path = write(tmp_path, {"way/1534356805": {**ENTRY, "drop": True}})
    result = OsmImporter().run(make_test_context(input_path=FIX4), importer_args(path))
    dropped = record(result, "way/1534356805")
    assert dropped.external_ids["osm"] == ["way/1534356805"]
    assert dropped.scope == "out_of_scope"
    (item,) = items(result, "out_of_scope", "way/1534356805")
    assert "dropped from scope" in item.reason and "the source says so" in item.reason
    assert record(result, "way/1534356804").external_ids["osm"] == [
        "way/1534356804",
        "way/1544360250",
    ]
    assert any(s.url.host == "example.com" for s in dropped.sources)


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ({"way/1": {**ENTRY}}, "must drop the element or replace"),
        ({"way/1": {**ENTRY, "drop": True, "name": "X"}}, "replaces nothing"),
        ({"way/1": {**ENTRY, "operator": "X", "colour": "red"}}, "Extra inputs"),
        ({"way/1": {**ENTRY, "name": "X", "quote": ""}}, "quote"),
        ({"Way 1": {**ENTRY, "name": "X"}}, "keys must be OSM refs"),
        ({"way/1": {**ENTRY, "name": "X", "reviewed_at": "soon"}}, "reviewed_at"),
    ],
)
def test_a_malformed_override_is_an_error(
    tmp_path: Path, entries: dict[str, object], message: str
) -> None:
    with pytest.raises(FetchError, match=message):
        load_overrides(write(tmp_path, entries))


def test_apply_overrides_replaces_and_removes_tags() -> None:
    objects, _ = parse_overpass(json.loads(FIX4.read_text(encoding="utf-8")))
    entries = load_overrides(REPO_OVERRIDES)
    kept, dropped = apply_overrides(objects, entries)
    by_ref = {o.ref: o for o in kept}
    assert dropped == []
    assert by_ref["way/293211687"].tags["operator"] == "Sabey Data Centers"
    assert "operator:wikidata" not in by_ref["way/293211687"].tags
    assert "name" not in by_ref["way/439365340"].tags
    assert (
        by_ref["way/635022480"].operator == "GI Partners" and by_ref["way/635022480"].name is None
    )


def test_every_committed_override_is_cited() -> None:
    """Each entry names its source and the verbatim quote that states the correction, when it
    was read and reviewed, and why; a page that refuses automated clients is cited with the
    snapshot read. No entry holds a person's details."""
    raw = json.loads(REPO_OVERRIDES.read_text(encoding="utf-8"))
    entries = load_overrides(REPO_OVERRIDES)
    assert len(entries) == len(raw) >= 1
    now = datetime.now(UTC)
    for ref, ov in entries.items():
        assert re.match(r"^(node|way|relation)/\d+$", ref)
        for key in ("reason", "source_url", "publisher", "quote", "retrieved_at", "reviewed_at"):
            assert raw[ref].get(key), (ref, key)
        assert 0 < len(ov.quote) <= 300 and ov.retrieved_at <= now, ref
        assert ov.reviewed_at <= now.date() and ov.retrieved_at.date() <= ov.reviewed_at, ref
        for text in (ov.quote, ov.reason, ov.name or "", ov.operator or ""):
            assert find_personal_data(text) == [], ref
        if ov.archive_url is not None:
            assert str(ov.archive_url).startswith("https://web.archive.org/web/"), ref
            assert str(ov.archive_url).endswith(str(ov.source_url)), ref
    assert list(raw) == sorted(raw)
    assert REPO_OVERRIDES.read_text(encoding="utf-8") == (
        json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def test_the_importer_fails_on_an_unknown_ref_before_fetching(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    """A stale entry (its element deleted or retagged upstream) stops the import, so a reviewer
    looks at it; it never silently stops applying."""
    path = write(tmp_path, {"node/1": {**ENTRY, "name": "X"}})
    with pytest.raises(FetchError, match="node/1"):
        OsmImporter().run(make_test_context(input_path=FIX4), importer_args(path))
    path = write(tmp_path, {"node/1": {**ENTRY}})
    with pytest.raises(FetchError, match="not a valid overrides file"):
        OsmImporter().run(make_test_context(input_path=FIX4), importer_args(path))
    assert NO_OVERRIDES.read_text(encoding="utf-8").strip() == "{}"
