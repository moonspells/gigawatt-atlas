from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from atlas.dissolve import (
    NEIGHBOR_GAP_M,
    Bounds,
    Cluster,
    ObjectKind,
    OsmObject,
    dissolve,
    haversine_m,
    meters_to_degrees,
    operators_compatible,
    ref_key,
)
from atlas.sources.osm import parse_overpass

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "osm" / "overpass-sample.json"
CASES = FIXTURE.with_name("overpass-cases.json")
LAT, LON = 39.0, -77.45


def offset(north_m: float, east_m: float) -> tuple[float, float]:
    """A point north_m and east_m metres from (LAT, LON)."""
    dlat, dlon = meters_to_degrees(1.0, LAT)
    return LAT + north_m * dlat, LON + east_m * dlon


def box(south_m: float, west_m: float, north_m: float, east_m: float) -> Bounds:
    s, w = offset(south_m, west_m)
    n, e = offset(north_m, east_m)
    return Bounds(s, w, n, e)


def obj(
    ref: str,
    *,
    at: tuple[float, float] = (0.0, 0.0),
    bounds: Bounds | None = None,
    kind: ObjectKind | None = None,
    **tags: str,
) -> OsmObject:
    """A test object at `at` metres from (LAT, LON), or at the center of bounds."""
    if bounds is not None:
        lat, lon = bounds.center
    else:
        lat, lon = offset(*at)
    if kind is None:
        kind = "point" if ref.startswith("node/") else "building"
    return OsmObject(
        ref, lat, lon, bounds, {k.replace("__", ":"): v for k, v in tags.items()}, kind
    )


def groups(clusters: list[Cluster]) -> set[frozenset[str]]:
    return {frozenset(c.refs) for c in clusters}


def fixture_objects(path: Path = FIXTURE) -> list[OsmObject]:
    objects, skipped = parse_overpass(json.loads(path.read_text(encoding="utf-8")))
    assert skipped == []
    return objects


def test_haversine_and_degrees() -> None:
    lat, lon = offset(300, 0)
    assert haversine_m(LAT, LON, lat, lon) == pytest.approx(300, rel=1e-3)
    lat, lon = offset(0, 300)
    assert haversine_m(LAT, LON, lat, lon) == pytest.approx(300, rel=1e-3)
    assert box(0, 0, 100, 200).area_m2() == pytest.approx(20_000, rel=1e-2)


def test_box_gaps() -> None:
    assert box(0, 0, 10, 10).gap_m(box(10, 0, 20, 10)) == 0.0  # touching
    assert box(0, 0, 10, 10).gap_m(box(5, 5, 20, 20)) == 0.0  # overlapping
    assert box(0, 0, 10, 10).gap_m(box(40, 0, 50, 10)) == pytest.approx(30, rel=1e-3)
    assert box(0, 0, 10, 10).gap_m(box(40, 50, 60, 60)) == pytest.approx(50, rel=1e-3)  # 30 by 40
    node = obj("node/1", at=(0, 100))
    assert node.extent == Bounds(node.lat, node.lon, node.lat, node.lon)
    assert node.gap_m(obj("way/2", bounds=box(-10, 0, 10, 60))) == pytest.approx(40, rel=1e-3)


def test_campus_containment_with_30_m_margin() -> None:
    campus = obj("way/1", bounds=box(0, 0, 400, 400), kind="campus")
    inside = obj("way/2", at=(200, 200), bounds=box(190, 190, 210, 210))
    margin = obj("node/3", at=(420, 200), name="Margin")  # 20 m north: within the 30 m margin
    outside = obj("node/4", at=(450, 200), name="Outside")  # 50 m north: outside
    clusters = dissolve([campus, inside, margin, outside])
    assert groups(clusters) == {
        frozenset({"way/1", "way/2", "node/3"}),
        frozenset({"node/4"}),
    }


def test_smallest_campus_wins_and_campuses_never_join() -> None:
    big = obj("way/10", bounds=box(0, 0, 1000, 1000), kind="campus")
    small = obj("way/11", bounds=box(400, 400, 600, 600), kind="campus")
    building = obj("way/12", bounds=box(490, 490, 510, 510))
    elsewhere = obj("way/13", bounds=box(90, 90, 110, 110))
    clusters = dissolve([big, small, building, elsewhere])
    # The small campus lies inside the big one, but campus objects never join each other.
    assert groups(clusters) == {
        frozenset({"way/11", "way/12"}),
        frozenset({"way/10", "way/13"}),
    }


def test_containment_needs_compatible_operators() -> None:
    campus = obj(
        "way/1",
        bounds=box(0, 0, 400, 400),
        kind="campus",
        operator="Amazon Web Services",
        operator__wikidata="Q456157",
    )
    other = obj("node/2", at=(100, 100), operator="Equinix", operator__wikidata="Q851641")
    unknown = obj("node/3", at=(200, 200))
    same_qid = obj("node/4", at=(300, 300), operator="AWS", operator__wikidata="Q456157")
    same_name = obj("node/5", at=(300, 100), operator="Amazon Web Services, Inc.")
    clusters = dissolve([campus, other, unknown, same_qid, same_name])
    assert groups(clusters) == {
        frozenset({"way/1", "node/3", "node/4", "node/5"}),
        frozenset({"node/2"}),
    }


def test_operator_guard_rules() -> None:
    campus = obj("way/1", bounds=box(0, 0, 400, 400), kind="campus", operator="Amazon")
    aws = obj("way/2", bounds=box(100, 100, 120, 120), operator="Amazon Web Services, Inc.")
    region = obj("node/3", at=(150, 300), operator="Amazon Web Services us-east-2 datacenter")
    other = obj("node/4", at=(300, 300), operator="Amazonia Hosting")  # not a word prefix
    assert groups(dissolve([campus, aws, region, other])) == {
        frozenset({"way/1", "way/2", "node/3"}),
        frozenset({"node/4"}),
    }
    # When both carry operator:wikidata, the ids decide, whatever the names say.
    q_campus = obj("way/5", bounds=box(0, 0, 400, 400), kind="campus", operator__wikidata="Q1")
    assert not operators_compatible(obj("node/6", operator="X", operator__wikidata="Q2"), q_campus)
    assert operators_compatible(obj("node/7", operator="Y", operator__wikidata="Q1"), q_campus)
    # One side has only an id and the other only a name: nothing to compare, so they join.
    assert operators_compatible(obj("node/8", operator="Equinix"), q_campus)
    assert operators_compatible(obj("node/9"), campus)


def test_operator_distance_is_between_boxes() -> None:
    # Two halls that touch: their centers are 320 m apart, their boxes 0 m.
    a = obj("way/1", bounds=box(0, 0, 320, 100), operator="Google")
    b = obj("way/2", bounds=box(320, 0, 640, 100), operator="Google")
    c = obj("way/3", bounds=box(941, 0, 1000, 100), operator="Google")  # 301 m north of b
    d = obj("way/4", bounds=box(320, 399, 640, 450), operator="Google")  # 299 m east of b
    assert haversine_m(a.lat, a.lon, b.lat, b.lon) == pytest.approx(320, rel=1e-3)
    assert a.gap_m(b) == 0.0
    assert groups(dissolve([a, b, c, d])) == {
        frozenset({"way/1", "way/2", "way/4"}),
        frozenset({"way/3"}),
    }


def test_operator_radius() -> None:
    a = obj("way/1", at=(0, 0), operator="Equinix")
    b = obj("way/2", at=(250, 0), operator="Equinix, Inc.")  # normalize_org drops "Inc."
    c = obj("way/3", at=(250, 299), operator="EQUINIX")  # 299 m from b
    d = obj("way/4", at=(250, 610), operator="Equinix")  # 311 m from c
    e = obj("way/5", at=(0, 100), operator="Digital Realty")
    clusters = dissolve([a, b, c, d, e])
    assert groups(clusters) == {
        frozenset({"way/1", "way/2", "way/3"}),
        frozenset({"way/4"}),
        frozenset({"way/5"}),
    }
    assert groups(dissolve([a, b, c, d, e], radius_m=400)) == {
        frozenset({"way/1", "way/2", "way/3", "way/4"}),
        frozenset({"way/5"}),
    }


def test_operator_radius_by_wikidata() -> None:
    a = obj("way/1", at=(0, 0), operator="Vadata", operator__wikidata="Q456157")
    b = obj("way/2", at=(100, 0), operator="Amazon Web Services", operator__wikidata="Q456157")
    assert groups(dissolve([a, b])) == {frozenset({"way/1", "way/2"})}


def test_neighbours_without_operator() -> None:
    assert NEIGHBOR_GAP_M == 50.0
    a = obj("way/1", bounds=box(0, 0, 20, 20))  # unnamed
    b = obj("way/2", bounds=box(0, 70, 20, 90))  # unnamed, 50 m east of a
    c = obj("way/3", bounds=box(0, 141, 20, 160))  # unnamed, 51 m east of b
    d = obj("way/4", bounds=box(40, 0, 60, 20), name="Hall D")  # 20 m north of a, but named
    e = obj("way/5", bounds=box(80, 0, 100, 20), name="hall d.")  # same normalized name as d
    f = obj("way/6", bounds=box(120, 0, 140, 20), name="Hall F")  # another name
    g = obj("node/7", at=(10, 45), operator="Equinix")  # between a and b, with an operator
    h = obj("node/8", at=(10, 30), operator__wikidata="Q851641")  # an id counts as an operator
    assert groups(dissolve([a, b, c, d, e, f, g, h])) == {
        frozenset({"way/1", "way/2"}),
        frozenset({"way/3"}),
        frozenset({"way/4", "way/5"}),
        frozenset({"way/6"}),
        frozenset({"node/7"}),
        frozenset({"node/8"}),
    }


def test_neighbours_without_operator_by_address() -> None:
    def plant(ref: str, bounds: Bounds, name: str, street: str = "Frontier Avenue") -> OsmObject:
        return obj(ref, bounds=bounds, name=name, addr__housenumber="5380", addr__street=street)

    a = plant("way/1", box(0, 0, 20, 20), "Plant A")
    b = plant("way/2", box(0, 170, 20, 190), "Plant B", "frontier avenue")  # 150 m east of a
    c = plant("way/3", box(0, 491, 20, 510), "Plant C")  # 301 m east of b
    d = obj(
        "way/4",
        bounds=box(30, 0, 50, 20),
        name="Plant D",
        operator="Example",  # has an operator
        addr__housenumber="5380",
        addr__street="Frontier Avenue",
    )
    e = obj("way/5", bounds=box(-40, 0, -20, 20), name="Plant E", addr__street="Frontier Avenue")
    assert groups(dissolve([a, b, c, d, e])) == {
        frozenset({"way/1", "way/2"}),
        frozenset({"way/3"}),
        frozenset({"way/4"}),
        frozenset({"way/5"}),
    }


def test_campus_objects_do_not_join_by_operator() -> None:
    a = obj("way/1", bounds=box(0, 0, 50, 50), kind="campus", operator="Google")
    b = obj("way/2", bounds=box(100, 100, 150, 150), kind="campus", operator="Google")
    assert groups(dissolve([a, b])) == {frozenset({"way/1"}), frozenset({"way/2"})}


def test_representative_choice() -> None:
    campus = obj("way/50", bounds=box(0, 0, 100, 100), kind="campus")
    big = obj("way/20", bounds=box(10, 10, 90, 90), operator="X")
    (cluster,) = dissolve([big, campus])
    assert cluster.representative.ref == "way/50"

    small = obj("way/30", bounds=box(0, 0, 20, 20), operator="X")
    large = obj("relation/31", bounds=box(30, 30, 80, 80), operator="X")
    node = obj("node/1", at=(40, 40), operator="X")
    (cluster,) = dissolve([small, large, node])
    assert cluster.representative.ref == "relation/31"

    nodes = [obj(f"node/{i}", at=(i, 0), operator="X") for i in (10, 9, 100)]
    (cluster,) = dissolve(nodes)
    assert cluster.representative.ref == "node/9"  # numeric order, not string order
    assert cluster.refs == ("node/9", "node/10", "node/100")


def test_members_sorted_and_clusters_ordered() -> None:
    objects = fixture_objects()
    clusters = dissolve(objects)
    for cluster in clusters:
        assert list(cluster.refs) == sorted(cluster.refs, key=ref_key)
        assert cluster.representative in cluster.members
    reps = [c.representative.ref for c in clusters]
    assert reps == sorted(reps, key=ref_key)
    assert sum(len(c.members) for c in clusters) == len(objects)


@pytest.mark.parametrize("path", [FIXTURE, CASES], ids=["sample", "cases"])
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_order_independent(seed: int, path: Path) -> None:
    objects = fixture_objects(path)
    expected = dissolve(objects)
    shuffled = list(objects)
    random.Random(seed).shuffle(shuffled)  # noqa: S311  (a test shuffle, not security)
    assert dissolve(shuffled) == expected


def test_fixture_clusters() -> None:
    by_rep = {c.representative.ref: c for c in dissolve(fixture_objects())}
    # The two AWS campus polygons hold their buildings, and IAD-80's box lies within 300 m of
    # IAD-71's, so the two campuses form one site (their centers are 413 m apart) ...
    assert by_rep["way/460053030"].refs == (
        "way/460053028",
        "way/460053030",
        "way/463571875",
        "way/463571876",
        "way/556599693",
        "way/556599694",
        "way/556599695",
        "way/596690174",
    )
    # ... but not Equinix DC17 and DC18, whose centers fall in the first campus's bounding box:
    # they join the Equinix buildings by operator instead.
    equinix = next(c for c in by_rep.values() if "node/14156477686" in c.refs)
    assert set(equinix.refs) == {
        "node/14156477686",
        "way/460050155",
        "way/596690175",
        "way/793888408",
        "way/1123249721",
        "way/1543931283",
    }
    assert by_rep["way/300163083"].refs == ("way/300163083", "way/610977485")  # NTT VA1, VA2
    assert len(by_rep) == 12


def test_case_fixture_clusters() -> None:
    by_refs = {c.refs: c for c in dissolve(fixture_objects(CASES))}
    # Rule 3: ten unnamed buildings without an operator in Dickey County, ND, one site.
    dickey = tuple(f"way/{i}" for i in range(1422191468, 1422191478))
    assert dickey in by_refs
    # Rule 2 between boxes: two Google halls that touch, with centers 320 m apart.
    assert ("way/844352473", "way/844352474") in by_refs
    # The operator guard reads "Amazon Web Services us-east-2 datacenter" as AWS.
    hilliard = by_refs[
        (
            "way/460067225",
            "way/673532692",
            "way/874506250",
            "way/975484796",
            "way/975484797",
            "way/975484798",
        )
    ]
    assert hilliard.representative.ref == "way/460067225"
    # Rule 3 by address: two Blockfusion buildings at 5380 Frontier Avenue, 154 m apart.
    assert ("way/832656125", "way/1229749890") in by_refs
    # Unnamed buildings 104 m apart stay apart; 22 m apart they join.
    assert ("way/1530966380",) in by_refs
    assert ("way/1530966381", "way/1530966382") in by_refs
    # An unnamed way without an operator does not join a building that has one.
    assert ("way/300974499",) in by_refs and ("way/567575425",) in by_refs
    assert len(by_refs) == 11


def test_bad_input() -> None:
    a = obj("way/1")
    with pytest.raises(ValueError, match="duplicate"):
        dissolve([a, a])
    with pytest.raises(ValueError, match="positive"):
        dissolve([a], radius_m=0)
    assert dissolve([]) == []
