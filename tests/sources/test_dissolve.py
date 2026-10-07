from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from atlas.dissolve import (
    Bounds,
    Cluster,
    ObjectKind,
    OsmObject,
    dissolve,
    haversine_m,
    meters_to_degrees,
    ref_key,
)
from atlas.sources.osm import parse_overpass

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "osm" / "overpass-sample.json"
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


def fixture_objects() -> list[OsmObject]:
    objects, skipped = parse_overpass(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert skipped == []
    return objects


def test_haversine_and_degrees() -> None:
    lat, lon = offset(300, 0)
    assert haversine_m(LAT, LON, lat, lon) == pytest.approx(300, rel=1e-3)
    lat, lon = offset(0, 300)
    assert haversine_m(LAT, LON, lat, lon) == pytest.approx(300, rel=1e-3)
    assert box(0, 0, 100, 200).area_m2() == pytest.approx(20_000, rel=1e-2)


def test_campus_containment_with_30_m_margin() -> None:
    campus = obj("way/1", bounds=box(0, 0, 400, 400), kind="campus")
    inside = obj("way/2", at=(200, 200), bounds=box(190, 190, 210, 210))
    margin = obj("node/3", at=(420, 200))  # 20 m north of the box: within the 30 m margin
    outside = obj("node/4", at=(450, 200))  # 50 m north: outside
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


def test_no_join_without_operator() -> None:
    a = obj("way/1", at=(0, 0), name="Unnamed hall A")
    b = obj("way/2", at=(20, 0), name="Unnamed hall A")
    c = obj("node/3", at=(10, 10), operator="Equinix")
    assert groups(dissolve([a, b, c])) == {
        frozenset({"way/1"}),
        frozenset({"way/2"}),
        frozenset({"node/3"}),
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


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_order_independent(seed: int) -> None:
    objects = fixture_objects()
    expected = dissolve(objects)
    shuffled = list(objects)
    random.Random(seed).shuffle(shuffled)  # noqa: S311  (a test shuffle, not security)
    assert dissolve(shuffled) == expected


def test_fixture_clusters() -> None:
    by_rep = {c.representative.ref: c for c in dissolve(fixture_objects())}
    # The AWS campus polygon holds its three buildings ...
    assert by_rep["way/460053028"].refs == (
        "way/460053028",
        "way/463571875",
        "way/463571876",
        "way/596690174",
    )
    # ... but not Equinix DC17 and DC18, whose centers fall in its bounding box: they join the
    # Equinix buildings by operator instead.
    equinix = next(c for c in by_rep.values() if "node/14156477686" in c.refs)
    assert set(equinix.refs) == {
        "node/14156477686",
        "way/460050155",
        "way/596690175",
        "way/793888408",
        "way/1123249721",
        "way/1543931283",
    }
    assert by_rep["way/460053030"].refs == (
        "way/460053030",
        "way/556599693",
        "way/556599694",
        "way/556599695",
    )
    assert len(by_rep) == 14


def test_bad_input() -> None:
    a = obj("way/1")
    with pytest.raises(ValueError, match="duplicate"):
        dissolve([a, a])
    with pytest.raises(ValueError, match="positive"):
        dissolve([a], radius_m=0)
    assert dissolve([]) == []
