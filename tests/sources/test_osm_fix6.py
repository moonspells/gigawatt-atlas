"""Regression tests for the OSM importer rules of fix round 6, each measured on the seed's inputs
against the review of the fifth seed (the owner's correction a rule overturns is named by its
record id). The elements are in tests/fixtures/osm/overpass-fix6.json, copied from the seed's
Overpass response; tests/test_dissolve_fix6.py tests the dissolve rules on their own."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from atlas.dissolve import Bounds, Cluster, OsmObject, operator_words
from atlas.geo.counties import CountyIndex
from atlas.schema.record import FacilityRecord
from atlas.sources.base import ImportContext, ImportResult, ReviewItem
from atlas.sources.osm import (
    OVERRIDES_PATH,
    ElementOverride,
    OsmImporter,
    campus_name,
    override_name_items,
    scope_doubt,
    telecom_site,
)
from atlas.validate import validate_record

MakeContext = Callable[..., ImportContext]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIX6 = FIXTURES / "osm" / "overpass-fix6.json"
FIX5 = FIXTURES / "osm" / "overpass-fix5.json"
NO_OVERRIDES = FIXTURES / "osm" / "overrides-empty.json"
REPO_OVERRIDES = Path(__file__).resolve().parents[2] / OVERRIDES_PATH


def importer_args(overrides: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    return parser.parse_args(["--no-pnnl", "--overrides", str(overrides)])


@pytest.fixture
def run(make_test_context: MakeContext) -> ImportResult:
    """The importer on the fix6 fixture, without PNNL and without overrides."""
    return OsmImporter().run(make_test_context(input_path=FIX6), importer_args(NO_OVERRIDES))


def record(result: ImportResult, ref: str) -> FacilityRecord:
    (found,) = [c.record for c in result.candidates if ref in c.match_values]
    return found


def items(result: ImportResult, kind: str, external_id: str) -> list[ReviewItem]:
    return [i for i in result.review if i.kind == kind and i.external_id == external_id]


def way(name: str, building: str = "yes", side_m: float = 40.0, **tags: str) -> OsmObject:
    """A square way with a name and telecom=data_center."""
    d = side_m / 111_195
    bounds = Bounds(40.0, -100.0, 40.0 + d, -100.0 + d)
    found = {"name": name, "telecom": "data_center", "building": building, **tags}
    return OsmObject("way/1", *bounds.center, bounds, found, "building")


def node(name: str, **tags: str) -> OsmObject:
    return OsmObject("node/1", 40.0, -100.0, None, {"name": name, **tags}, "point")


def alone(obj: OsmObject) -> Cluster:
    return Cluster(members=(obj,), representative=obj)


def test_every_record_validates(run: ImportResult, counties: CountyIndex, today: date) -> None:
    for c in run.candidates:
        assert validate_record(c.record, counties=counties, today=today) == [], c.match_values


# ---------------------------------------------------------------------------- rule A


def test_computer_words_alone_do_not_keep_a_business_in_scope(run: ImportResult) -> None:
    """'All About Serving Process Server' (a process-serving business, owner's correction
    gwa-01m4hfqm18z5bp8nx7zyyf3h50) and 'QES Computers' (an 87 m² shop,
    gwa-01m4hfqmxgm9v95dxj68532bnf) were published: 'server' and 'computers' kept them in
    scope against the later doubt rules."""
    found = {
        "node/7578821329": "a point with a business name and no operator",
        "way/358946546": "covers under 200 m², a hut or a room",
        "node/11938614120": "a point with a business name and no operator",  # Alembic Computer
        "node/3468203102": "a point with a business name and no operator",  # LSU's CCT
    }
    for ref, why in found.items():
        assert record(run, ref).scope == "out_of_scope", ref
        (item,) = items(run, "out_of_scope", ref)
        assert why in item.reason and "until a reviewer decides" in item.reason


def test_a_compute_center_and_a_data_center_name_stay_in_scope(run: ImportResult) -> None:
    """'Iron City No. 1 Power Generator and Compute Center' and 'Date Center West - Eugene'
    (operator Data Center West, Inc.) still say compute center and data center."""
    for ref in ("node/13908533343", "node/13748859030"):
        assert record(run, ref).scope == "in_scope", ref


@pytest.mark.parametrize(
    ("name", "doubted"),
    [
        ("Acme Computers", True),
        ("Acme Computing Solutions", True),
        ("Acme Process Server", True),
        ("Acme Server Farm", False),
        ("Acme Computing Center", False),
        ("Acme Colocation", False),
        ("Acme Data Centre", False),
    ],
)
def test_the_words_that_keep_a_business_point_in_scope(name: str, doubted: bool) -> None:
    assert (scope_doubt(alone(node(name, telecom="data_center")), frozenset()) is not None) is (
        doubted
    )


# ---------------------------------------------------------------------------- rule B1


def test_a_house_is_held_out_of_scope(run: ImportResult) -> None:
    """'Lumen' in St. George is a 169 m² building=house among 25 houses (owner's correction
    gwa-01m4hfqnrs9bsghhcr3mddwn6n)."""
    lumen = record(run, "way/835601088")
    assert lumen.scope == "out_of_scope"
    (item,) = items(run, "out_of_scope", "way/835601088")
    assert "way/835601088 is a house (building=house), not a data center building" in item.reason


def test_apartments_and_other_lumen_buildings_stay_in_scope(run: ImportResult) -> None:
    """Cologix Jacksonville JAX1 is tagged building=apartments; Lumen's Spokane, Phoenix and Tustin
    buildings are likely Lumen data centers ('Lumen' is no telephone brand of the screen)."""
    for ref in ("way/497151852", "way/444683815", "way/1052182309", "way/441502816"):
        assert record(run, ref).scope == "in_scope", ref


@pytest.mark.parametrize(
    ("building", "doubted"),
    [("house", True), ("detached", True), ("bungalow", True), ("apartments", False)],
)
def test_residential_building_types(building: str, doubted: bool) -> None:
    obj = way("Example Hosting", building, side_m=100.0)  # a word that keeps the later rules off
    assert (scope_doubt(alone(obj), frozenset()) is not None) is doubted


# ---------------------------------------------------------------------------- rule C


def test_a_satellite_teleport_is_a_telecom_site(run: ImportResult) -> None:
    """Encompass's Lino Lakes facility 'houses a state-of-the-art teleport' by its description
    (owner's correction gwa-01m4hfqpgbaf6wft2tjg5q1sms); SES's Hawley Earth Station is a
    satellite earth station."""
    for ref, words in (("way/1391966326", "'teleport'"), ("relation/10132610", "'Earth Station'")):
        rec = record(run, ref)
        assert (rec.scope, rec.purpose) == ("out_of_scope", "telecom"), ref
        (item,) = items(run, "out_of_scope", ref)
        assert f"named or described as a satellite teleport or earth station ({words})" in (
            item.reason
        )


def test_a_teleport_in_a_name_or_beside_data_center_words_stays_in_scope(
    run: ImportResult,
) -> None:
    """Telehouse Teleport (7 Teleport Drive, Staten Island) is a data center."""
    telehouse = record(run, "way/247428943")
    assert (telehouse.scope, telehouse.purpose) == ("in_scope", "unknown")
    hosted = way("Example Uplink", description="A teleport and colocation data center")
    assert telecom_site(alone(hosted)) is None
    assert telecom_site(alone(way("Example", description="A satellite uplink site"))) is not None


# ---------------------------------------------------------------------------- rule D


def test_a_construction_site_that_has_not_opened_is_under_construction(run: ImportResult) -> None:
    """PowerHouse CTP-02's site (landuse=construction, opening_date=2027) holds a hall tagged
    only telecom=data_center, which made the record operating (owner's correction
    gwa-01m4hfqpm8fn3m9qw3crhmkv0k)."""
    ctp = record(run, "way/1493522242")
    assert ctp.status == "under_construction"
    assert [(e.event, e.status, e.as_of.value, e.planned) for e in ctp.status_history] == [
        ("other", "under_construction", "2026-10-09", False),
        ("energized", "operating", "2027", True),
    ]
    assert "operating_since" not in ctp.dates
    (item,) = [
        i for i in items(run, "conflict", "way/1493522242") if "construction site" in i.reason
    ]
    assert item.data == {"refs": ["way/1449347339"], "opening_date": "2027"}
    assert "a reviewer checks whether a building operates" in item.reason


def test_construction_sites_without_a_future_opening_keep_their_status(run: ImportResult) -> None:
    """Meta Rosemount's construction polygon has no opening_date (the review accepted operating);
    Arcola Tech Park's opening_date=2030 is on a landuse=industrial polygon, which is not
    planned."""
    for ref in ("way/1333289545", "way/897226573"):
        assert record(run, ref).status == "operating", ref
        assert not [i for i in items(run, "conflict", ref) if "construction site" in i.reason]


# ---------------------------------------------------------------------------- rule E2


def test_a_building_outside_the_campus_polygon_is_its_own_record(run: ImportResult) -> None:
    """Way 45583037 (7777 Infantry Ridge Road, unnamed, no operator) lies 39 m from Equinix DC14
    but outside Equinix's site polygon (owner's correction gwa-01m4hfqp7w390ge6zatq94ey6t)."""
    dc14 = record(run, "way/1227539675")
    assert dc14.external_ids["osm"] == ["way/45583050", "way/1227539675"]
    aol = record(run, "way/45583037")
    assert aol.external_ids["osm"] == ["way/45583037"]
    assert aol.canonical_name.startswith("Data center (")
    assert (aol.status, aol.parties.operator) == ("operating", [])


def test_an_unnamed_neighbour_without_a_site_polygon_still_joins(run: ImportResult) -> None:
    """The unnamed way at 800 Guardians Way stays a building of Evoque Allen DA1 (900)."""
    allen = record(run, "way/254773897")
    assert allen.external_ids["osm"] == ["way/254773884", "way/254773897"]


# ---------------------------------------------------------------------------- rule F1


def test_a_landlords_buildings_leased_to_a_tenant_join_its_site(run: ImportResult) -> None:
    """Amazon IAD-6 and IAD-13 (operator AWS, owner=Digital Realty) in the Digital Loudoun Plaza
    polygons were a record of their own (owner's correction gwa-01m4hfqp14em6wf8wrm7ftzkvt)."""
    plaza = record(run, "way/1020953461")
    assert {"way/300969615", "way/460175672"} <= set(plaza.external_ids["osm"])
    assert len(plaza.external_ids["osm"]) == 12
    assert plaza.canonical_name.startswith("Digital Loudoun Plaza (")
    assert [o.name for o in plaza.parties.operator] == ["Digital Realty"]
    assert {"Amazon IAD-6", "Amazon IAD-13"} <= {b.name for b in plaza.buildings}
    assert "Amazon IAD-13 47 MW" in (plaza.capacity.mw_as_stated or "")
    assert not [i for i in run.review if i.kind == "possible_duplicate" and "mixed" in i.reason]


# ---------------------------------------------------------------------------- rule F2


def test_a_carrier_hotel_building_makes_no_record_of_its_own(run: ImportResult) -> None:
    """Lakeside Technology Center (350 East Cermak Road, no operator) holds the points of Digital
    Realty ORD10, Centersquare ORD1 and Equinix CH1 at its own address; its record counted the
    facility again beside ORD10's (owner's correction gwa-01m4hfqm43st3n6ehq12fz8s2t)."""
    assert not [c for c in run.candidates if "way/156520409" in c.match_values]
    (item,) = items(run, "possible_duplicate", "way/156520409")
    tenants = ["node/10910879064", "node/10910879065", "node/12695496267"]
    assert item.data["other"] == tenants
    assert "3 operators at its own address (Centersquare, Digital Realty, Equinix)" in item.reason
    for ref in tenants:
        assert record(run, ref).external_ids["osm"] == [ref]  # each tenant stays its own record


def test_a_building_with_its_own_operator_is_no_carrier_hotel(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    doc = json.loads(FIX6.read_text(encoding="utf-8"))
    for e in doc["elements"]:
        if e["id"] == 156520409:
            e["tags"]["operator"] = "Digital Realty"
    path = tmp_path / "overpass.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    result = OsmImporter().run(make_test_context(input_path=path), importer_args(NO_OVERRIDES))
    assert record(result, "way/156520409")
    assert not items(result, "possible_duplicate", "way/156520409")


# ---------------------------------------------------------------------------- rule 9 (F4b)


def test_one_campus_tied_two_ways_is_one_record(run: ImportResult) -> None:
    """CloudHQ LC1 to LC3 and LC4 to LC14 (650 m apart, the series LC3-LC4, Loudoun County Parkway
    and CloudHQ's LC campus page) were two records (owner's correction
    gwa-01m4hfqpdz4tepn3b5d6473r4n); STACK NVA04C's node between them does not keep them
    apart."""
    lc = record(run, "way/1343774537")
    assert len(lc.external_ids["osm"]) == 13
    assert lc == record(run, "node/14201428241")
    assert lc.canonical_name.startswith("CloudHQ Ashburn Campus (")
    assert [p.phase_id for p in lc.phases] == [
        "osm-operating",
        "osm-under_construction",
        "osm-announced",
    ]
    assert record(run, "node/14189562575").external_ids["osm"] == ["node/14189562575"]


# ---------------------------------------------------------------------------- rules H1, H2b


def test_a_code_word_name_does_not_stop_a_series(run: ImportResult) -> None:
    """'IAD' beside 'Aligned IAD-01' and 'Aligned IAD-02' named the campus after one building
    (owner's correction gwa-01m4hfqp3tm8je06mkdss3h3br)."""
    aligned = record(run, "way/1088241977")
    assert aligned.canonical_name.startswith("Aligned IAD-01 and IAD-02 (")


def test_names_are_compared_without_a_leading_operator(run: ImportResult) -> None:
    """'Digital Realty Silicon Valley SJC37' and 'Silicon Valley SJC31' (owner's correction
    gwa-01m4hfqpcgqy3zzvyrgd0fq2j0); the stem first would give the name of the Digital Realty
    campus 0.78 km away."""
    sjc = record(run, "way/1314012931")
    assert sjc.canonical_name.startswith("Digital Realty Silicon Valley SJC31 and SJC37 (")
    other = record(run, "way/209073641")
    assert other.canonical_name.startswith("Digital Realty Silicon Valley (")
    words = operator_words([way("x", operator="Digital Realty")])
    ops = frozenset({"digital realty"})
    names = ["Silicon Valley SJC31", "Silicon Valley SJC37"]
    assert campus_name(names, words, ops) == "Silicon Valley"
    assert campus_name(names, words, ops, series_first=True) == "Silicon Valley SJC31 and SJC37"


# ---------------------------------------------------------------------------- overrides


def ntt_entries(tmp_path: Path, *, va9: bool = False) -> Path:
    """An overrides file with the committed entry that names way/1188691868 'NTT VA8', and with
    va9 an entry that names way/1560827941 'NTT VA9'."""
    committed = json.loads(REPO_OVERRIDES.read_text(encoding="utf-8"))
    entries = {"way/1188691868": committed["way/1188691868"]}
    if va9:
        entries["way/1560827941"] = {**committed["way/1188691868"], "name": "NTT VA9"}
    path = tmp_path / "osm.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_an_entry_that_gives_another_elements_name_is_a_conflict(
    make_test_context: MakeContext, tmp_path: Path
) -> None:
    """The entry renames way/1188691868 'NTT VA8' while way/1560827941, 160 m away, is 'NTT VA8'
    in OpenStreetMap: the import goes on, with a conflict item."""
    path = ntt_entries(tmp_path)
    result = OsmImporter().run(make_test_context(input_path=FIX5), importer_args(path))
    (item,) = [i for i in items(result, "conflict", "way/1188691868") if "one name" in i.reason]
    assert item.data == {
        "osm": "way/1188691868",
        "other": "way/1560827941",
        "name": "NTT VA8",
        "gap_m": 160,
    }
    assert "which way/1560827941 also carries 160 m away" in item.reason
    # An entry that renames the other element settles it.
    fixed = OsmImporter().run(
        make_test_context(input_path=FIX5), importer_args(ntt_entries(tmp_path, va9=True))
    )
    assert not [i for i in fixed.review if i.kind == "conflict" and "one name" in i.reason]


def test_the_same_name_far_away_or_settled_is_no_conflict() -> None:
    cited = {
        "reason": "test",
        "source_url": "https://example.com/dc1",
        "publisher": "Example",
        "quote": "Example DC1",
        "retrieved_at": "2026-10-09T00:00:00Z",
        "reviewed_at": "2026-10-09",
    }
    entry = ElementOverride.model_validate({"name": "Example DC1", **cited})
    here = OsmObject("way/1", 40.0, -100.0, None, {"name": "Old"}, "building")
    near = OsmObject("way/2", 40.01, -100.0, None, {"name": "Example DC1"}, "building")
    far = OsmObject("way/3", 40.03, -100.0, None, {"name": "Example DC1"}, "building")
    found = override_name_items([here, near], {"way/1": entry})
    assert [(i.external_id, i.data["other"]) for i in found] == [("way/1", "way/2")]
    assert override_name_items([here, far], {"way/1": entry}) == []  # 3.3 km away
    # An entry that drops the other element, or renames it, settles the name.
    for other in ({"drop": True, **cited}, {"name": "Example DC2", **cited}):
        settled = {"way/1": entry, "way/2": ElementOverride.model_validate(other)}
        assert override_name_items([here, near], settled) == []
    # An entry that changes only the other element's operator does not.
    run_by = {"way/1": entry, "way/2": ElementOverride.model_validate({"operator": "X", **cited})}
    assert len(override_name_items([here, near], run_by)) == 1
