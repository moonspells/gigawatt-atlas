"""Regression tests for what the pre-check of the fourth seed import found in the OSM records
(fix round 5; n<number> is the finding in its bug list), and for the config/overrides/osm.json
entries of that round. The elements are in tests/fixtures/osm/overpass-fix5.json, copied from the
seed's Overpass response; the PNNL rows in tests/fixtures/pnnl/centroids-fix5.geojson."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from atlas.dissolve import Cluster, operator_words
from atlas.geo.counties import CountyIndex
from atlas.schema.record import FacilityRecord
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.sources.osm import (
    OVERRIDES_PATH,
    OsmImporter,
    OsmObject,
    campus_name,
    parse_overpass,
    scope_doubt,
)
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIX5 = FIXTURES / "osm" / "overpass-fix5.json"
FIX4 = FIXTURES / "osm" / "overpass-fix4.json"
PNNL_FIX5 = FIXTURES / "pnnl" / "centroids-fix5.geojson"
PNNL_FIX4 = FIXTURES / "pnnl" / "centroids-fix4.geojson"
NO_OVERRIDES = FIXTURES / "osm" / "overrides-empty.json"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / OVERRIDES_PATH
OSM = "https://www.openstreetmap.org/"


def importer_args(pnnl: Path, overrides: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    return parser.parse_args(["--pnnl", str(pnnl), "--overrides", str(overrides)])


@pytest.fixture
def run(make_test_context: MakeContext) -> ImportResult:
    """The importer on the fix5 fixture without overrides, as `atlas import osm --input ...
    --pnnl ...` runs it."""
    return OsmImporter().run(
        make_test_context(input_path=FIX5), importer_args(PNNL_FIX5, NO_OVERRIDES)
    )


@pytest.fixture
def run4(make_test_context: MakeContext) -> ImportResult:
    """The importer on the fix4 fixture with the committed overrides (every element they name is
    there)."""
    return OsmImporter().run(
        make_test_context(input_path=FIX4), importer_args(PNNL_FIX4, REPO_OVERRIDES)
    )


def record(result: ImportResult, ref: str) -> FacilityRecord:
    (found,) = [c.record for c in result.candidates if ref in c.match_values]
    return found


def rep_ref(r: FacilityRecord) -> str:
    """The representative's ref, from location.geometry_ref ("osm:way/1")."""
    return (r.location.geometry_ref or "").removeprefix("osm:")


def items(result: ImportResult, kind: str, external_id: str) -> list[ReviewItem]:
    return [i for i in result.review if i.kind == kind and i.external_id == external_id]


def test_every_record_validates(
    run: ImportResult, run4: ImportResult, counties: CountyIndex, today: date
) -> None:
    for result in (run, run4):
        for c in result.candidates:
            assert validate_record(c.record, counties=counties, today=today) == [], c.match_values


# ---------------------------------------------------------------------------- scope


def test_a_family_history_center_is_held_out_of_scope(run: ImportResult) -> None:
    """n3: way 903642490, 'Family History Center' of the Church of Jesus Christ of Latter-day
    Saints (telecom=data_center, 4,081 sq ft), was published as an operating data center."""
    fhc = record(run, "way/903642490")
    assert fhc.scope == "out_of_scope"
    (item,) = items(run, "out_of_scope", "way/903642490")
    assert "a room or a non-compute use ('Family History Center')" in item.reason


@pytest.mark.parametrize(
    "name", ["FamilySearch Center", "Genealogy Library", "Family History Centre"]
)
def test_genealogy_rooms_are_doubted(name: str) -> None:
    obj = OsmObject(
        "way/1",
        40.0,
        -100.0,
        None,
        {"building": "yes", "telecom": "data_center", "name": name},
        "building",
    )
    assert scope_doubt(Cluster(members=(obj,), representative=obj), frozenset()) is not None


# ---------------------------------------------------------------------------- one site, one record


def test_adjoining_sites_of_one_operator_are_one_record(run: ImportResult) -> None:
    """n7, n19: Meta New Albany and its LCO 3 expansion site (the outlines touch), and Google's
    two Lenoir outers 108 m apart (one multipolygon until 2026-07-10, PNNL's one 78.2-acre campus
    row), were two records each. The campus is named after the established site, not the
    expansion's project name, and the expansion site does not lead it."""
    meta = record(run, "way/1281938077")
    assert set(meta.external_ids["osm"]) == {
        "way/671301768",
        "way/708899010",
        "way/802160675",
        "way/1252196813",
        "way/1281938077",
        "way/1386016634",
        "way/1386016635",
    }
    assert meta.canonical_name.startswith("Meta New Albany Data Center (")
    assert meta.location.geometry_ref == "osm:way/708899010"
    assert "Meta LCO 3 Project" in [a.name for a in meta.aliases]
    google = record(run, "relation/9474864")
    assert google.external_ids["osm"] == [
        "way/186515922",
        "way/682979410",
        "way/844372538",
        "relation/9474864",
    ]
    assert google.canonical_name.startswith("Google Lenoir Data Center (")
    assert "Google Project Cardinal" in [a.name for a in google.aliases]
    # The PNNL row spans both outlines: it is matched, sets no acreage from one of them, and is
    # no longer filed as "another version of the polygon".
    assert google.site.acreage is None
    assert google.external_ids["pnnl_im3"] == ["campus@-81.546515,35.894738"]
    assert not [i for i in run.review if i.source == "pnnl" and "another version" in i.reason]


def test_a_numbered_sibling_just_beyond_the_radius_joins(run: ImportResult) -> None:
    """n4, n16: Iron Mountain VA-6 (364 m from VA-5, operator Q1673079, postcode 20109) was a
    record of its own beside 'Iron Mountain VA-2' (VA-1 to VA-5 and VA-7)."""
    iron = record(run, "way/1304357577")
    assert len(iron.external_ids["osm"]) == 7
    assert iron.canonical_name.startswith("Iron Mountain VA-1 to VA-7 (")
    assert sorted(a.name for a in iron.aliases) == [f"Iron Mountain VA-{i}" for i in range(1, 8)]


def test_a_same_street_building_just_beyond_the_radius_joins(run: ImportResult) -> None:
    """n18: Digital Realty IAD41 (Building P, 44751 Round Table Plaza) is 348 m from IAD39
    (Building L) and was its own record; Digital Realty's filing calls Buildings L, M, N and P one
    campus."""
    dlr = record(run, "way/793087858")
    assert dlr.external_ids["osm"] == [
        "way/701923202",
        "way/701923203",
        "way/793087858",
        "way/1518717349",
    ]
    assert dlr.canonical_name.startswith("Digital Realty IAD39, IAD40, IAD41 and IAD73 (")
    assert dlr.capacity.it_mw is None  # IAD41 has no it_power tag
    assert dlr.capacity.facility_mw == 450.0  # 192 + 60 + 63 + 135


def test_a_series_split_further_apart_is_a_review_item(run: ImportResult) -> None:
    """n8, n17: RagingWire CA3 is 720 m from CA2 (NTT: 'one of three data centers on our
    campus'); CloudHQ's LC campus is two records about 650 m apart. Neither was flagged."""
    assert record(run, "way/190868312") != record(run, "way/32560290")
    (item,) = items(run, "possible_duplicate", "way/190868312")
    assert item.data["other"] == "way/32560290"
    assert "'RagingWire CA3' continues the series of 'RagingWire CA2', 720 m apart" in item.reason
    lc = record(run, "way/1343774537")
    assert lc != record(run, "way/1560822523")
    assert lc.canonical_name.startswith("CloudHQ Ashburn Campus (")
    lc4 = record(run, "way/1560822523")
    reps = {rep_ref(lc), rep_ref(lc4)}
    (pair,) = [
        i
        for i in run.review
        if i.kind == "possible_duplicate" and {i.external_id, i.data.get("other")} == reps
    ]
    assert "may be one campus" in pair.reason


def test_an_unnamed_building_in_one_operators_row_joins_it(run: ImportResult) -> None:
    """n9: way 597876723 (904 Quality Way, no name, no operator) is 18 m from Digital Realty
    DFW18 among the six Digital Dallas buildings and was 'Data center (Richardson, TX)'. The
    Rackspace-tagged building 42 m from the row (Digital Realty's DFW25) stands, with an item."""
    dallas = record(run, "way/597876730")
    assert "way/597876723" in dallas.external_ids["osm"]
    assert len(dallas.external_ids["osm"]) == 7
    assert dallas.canonical_name.startswith("Digital Realty Dallas (")
    assert record(run, "way/597876719") != dallas
    (item,) = items(run, "possible_duplicate", "way/597876719")
    assert item.data["other"] == "way/597876730"
    assert "operator 'Rackspace'" in item.reason and "tenant" in item.reason


def test_a_building_drawn_twice_in_a_cluster_is_listed_once(run: ImportResult) -> None:
    """n14: Apple Mesa's site holds way 300974499 (Apple Data Center) and the unnamed,
    operator-less way 567575425 drawn on the same footprint; buildings[] listed both."""
    apple = record(run, "way/300974499")
    assert "way/567575425" in apple.external_ids["osm"]
    assert [b.ref for b in apple.buildings] == ["osm:way/300974499"]
    (item,) = [
        i
        for i in items(run, "possible_duplicate", rep_ref(apple))
        if i.data.get("osm") == "way/567575425"
    ]
    assert item.data["other"] == "way/300974499" and "mapped twice" in item.reason


# ---------------------------------------------------------------------------- names


def test_a_campus_is_not_named_after_one_building(run: ImportResult, run4: ImportResult) -> None:
    """n16: 'QTS NAL1 DC2' for DC1 and DC2 under QTS's unnamed construction polygon, 'Amazon
    IAD-127' for IAD-124 to IAD-127, 'PowerHouse Pacific Building 1' for Buildings 1 to 3. Every
    member name stays an alias."""
    qts = record(run, "way/1281938083")
    assert qts.canonical_name.startswith("QTS NAL1 (")
    assert sorted(a.name for a in qts.aliases) == ["QTS NAL1 DC1", "QTS NAL1 DC2"]
    aws = record(run4, "way/1301654223")
    assert aws.canonical_name.startswith("Amazon IAD-124 to IAD-127 (")
    assert "Amazon IAD-127" in [a.name for a in aws.aliases]
    powerhouse = record(run4, "way/1534356804")
    assert powerhouse.canonical_name.startswith("PowerHouse Pacific (")
    assert len(powerhouse.aliases) == 3


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["PowerHouse Pacific Building 1", "PowerHouse Pacific Building 2"], "PowerHouse Pacific"),
        (["QTS NAL1 DC2", "QTS NAL1 DC1"], "QTS NAL1"),
        (["QTS Ashburn 1 DC1", "QTS Ashburn 1 DC2"], "QTS Ashburn 1"),
        (["Centersquare IAD1-A", "Centersquare IAD1-B"], "Centersquare IAD1"),
        ([f"Amazon IAD-{i}" for i in (127, 124, 126, 125)], "Amazon IAD-124 to IAD-127"),
        (["NTT VA1", "NTT VA2"], "NTT VA1 and VA2"),
        (["Digital Realty IAD39", "Digital Realty IAD73"], "Digital Realty IAD39 and IAD73"),
        (
            [f"Digital Realty IAD{i}" for i in (73, 39, 41, 40)],
            "Digital Realty IAD39, IAD40, IAD41 and IAD73",
        ),
        ([f"CloudHQ LC{i}" for i in (4, 5, 8, 9, 10)], "CloudHQ LC4, LC5, LC8 and 2 more"),
        (["EAT12", "EAT13", "EAT14"], "EAT12 to EAT14"),
        (["EdgeConneX - DEN01", "EdgeConnex - DEN02"], "EdgeConneX DEN01 and DEN02"),
        (["H5 IAD31", "H5 IAD32"], "H5 IAD31 and IAD32"),
        (["Tech Park at Brambleton", "BlackChamber Tech Park at Brambleton"], None),
        (["CyrusOne Sterling IX", "CyrusOne NVA9"], None),
        (["Amazon IAD-61"], None),
    ],
)
def test_campus_name(names: list[str], expected: str | None) -> None:
    operators = frozenset(
        {
            "amazon web services",
            "cloudhq",
            "cyrusone",
            "digital realty",
            "edgeconnex",
            "h5 data centers",
            "ntt",
            "qts",
        }
    )
    words = frozenset(w for op in operators for w in op.split()) | {"amazon", "powerhouse"}
    assert campus_name(names, words, operators) == expected


# ---------------------------------------------------------------------------- PNNL


def test_a_pnnl_row_with_another_building_number_is_a_conflict(run: ImportResult) -> None:
    """n13: PNNL's row 'NTT Ashburn VA8 Data Centre' lies on way 1188691868, which OSM renamed
    'NTT VA9' on 2026-07-24; the import published VA9 as operating and filed nothing."""
    (item,) = items(run, "conflict", "building@-77.475531,39.020190")
    assert item.source == "pnnl" and item.data["osm"] == "way/1188691868"
    assert "building VA8 or VA9?" in item.reason


# ---------------------------------------------------------------------------- citations


def _element_tags(path: Path) -> dict[str, dict[str, str]]:
    objects, _ = parse_overpass(json.loads(path.read_text(encoding="utf-8")))
    return {o.ref: dict(o.tags) for o in objects}


def _osm_ref(record: FacilityRecord, sid: str) -> str | None:
    (source,) = [s for s in record.sources if s.id == sid]
    url = str(source.url)
    return url.removeprefix(OSM) if url.startswith(OSM) else None


def test_every_value_cites_the_element_that_states_it(run: ImportResult) -> None:
    """n6, n11, n12: aliases, buildings, the operator, the street, the power tags and the status
    came from cluster members, but every record cited only its representative (s1): Canton's
    construction status from a site polygon without a building tag, Flexential Brentwood's
    operator and street from a polygon that has neither."""
    tags = _element_tags(FIX5)
    for c in run.candidates:
        r = c.record
        for alias in r.aliases:
            refs = [_osm_ref(r, sid) for sid in alias.source_ids]
            assert refs and all(ref and tags[ref].get("name") == alias.name for ref in refs), alias
        for org in r.parties.operator:
            refs = [_osm_ref(r, sid) for sid in org.source_ids]
            assert refs and all(ref and tags[ref].get("operator") == org.name for ref in refs)
        for i, b in enumerate(r.buildings):
            backing = [s for s in r.sources if f"/buildings/{i}" in s.supports]
            assert [str(s.url) for s in backing] == [OSM + b.ref.removeprefix("osm:")]
        for pointer, key in (("/location/street", "addr:street"), ("/capacity/mw_as_stated", "")):
            for s in r.sources:
                if pointer in s.supports and (ref := _osm_ref(r, s.id)) is not None:
                    if key:
                        street = r.location.street or ""
                        assert (tags[ref].get(key) or "-") in street, (ref, pointer)
                    else:
                        assert "it_power" in tags[ref] or "input:electricity" in tags[ref]
    canton = record(run, "way/1459880367")
    (event,) = canton.status_history
    assert event.status == "under_construction"
    assert sorted(_osm_ref(canton, sid) or "" for sid in event.source_ids) == [
        f"way/{i}" for i in range(1430720170, 1430720175)
    ]
    assert "way/1459880367" not in [_osm_ref(canton, sid) for sid in event.source_ids]
    flexential = record(run, "way/826001604")
    ((op, sids),) = [(o.name, o.source_ids) for o in flexential.parties.operator]
    assert op == "Flexential" and [_osm_ref(flexential, s) for s in sids] == ["node/12112295055"]
    on_street = [s for s in flexential.sources if "/location/street" in s.supports]
    assert [str(s.url) for s in on_street] == [OSM + "node/12112295055"]
    s1 = flexential.sources[0]
    assert str(s1.url) == OSM + "way/826001604" and "/location/street" not in s1.supports


def test_operator_words_name_no_campus() -> None:
    objects, _ = parse_overpass(json.loads(FIX5.read_text(encoding="utf-8")))
    assert {"iron", "mountain", "cloudhq"} <= operator_words(objects)
