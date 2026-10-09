"""The dissolve rules added after the seed check of 2026-10-08 (atlas/dissolve.py rules 1, 4, 5
and 6, and the region guard). tests/sources/test_dissolve.py covers the rules before it."""

from __future__ import annotations

from atlas.dissolve import (
    NAME_GAP_M,
    Bounds,
    Cluster,
    ObjectKind,
    OsmObject,
    dissolve,
    meters_to_degrees,
    operator_conflicts,
    point_in_outline,
    specific_name,
)

LAT, LON = 39.0, -77.45


def offset(north_m: float, east_m: float) -> tuple[float, float]:
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
    outline: tuple[tuple[tuple[float, float], ...], ...] | None = None,
    **tags: str,
) -> OsmObject:
    if bounds is not None:
        lat, lon = bounds.center
    else:
        lat, lon = offset(*at)
    if kind is None:
        kind = "point" if ref.startswith("node/") else "building"
    tags = {k.replace("__", ":"): v for k, v in tags.items()}
    return OsmObject(ref, lat, lon, bounds, tags, kind, outline)


def groups(clusters: list[Cluster]) -> set[frozenset[str]]:
    return {frozenset(c.refs) for c in clusters}


def ring(*corners: tuple[float, float]) -> tuple[tuple[float, float], ...]:
    points = tuple(offset(*c) for c in corners)
    return (*points, points[0])


def test_no_join_crosses_a_region() -> None:
    """Seed check n10: rule 2 joined xAI's building in DeSoto County, MS, into a Shelby County,
    TN, record. With regions (ref -> county FIPS) no rule joins two regions, and a chain cannot
    bridge them through a third object."""
    a = obj("way/1", bounds=box(0, 0, 20, 20), operator="xAI")
    b = obj("way/2", bounds=box(0, 120, 20, 140), operator="xAI")  # 100 m east, across the line
    c = obj("way/3", bounds=box(0, 60, 20, 80), operator="xAI")  # between, on a's side
    regions = {"way/1": "47157", "way/2": "28033", "way/3": "47157"}
    assert groups(dissolve([a, b, c])) == {frozenset({"way/1", "way/2", "way/3"})}
    assert groups(dissolve([a, b, c], regions=regions)) == {
        frozenset({"way/1", "way/3"}),
        frozenset({"way/2"}),
    }


def test_a_site_holds_what_lies_in_its_outline() -> None:
    """Seed check n44, n48, n53, n71, n79: site polygons (industrial=data_centre) were not
    fetched, so Microsoft Cheyenne and Bison and Project Cardinal split into unnamed records. A
    site joins every object whose center lies in its outline (or, without one, its box + 30 m),
    campuses too, unless the operators disagree."""
    # An L-shaped site: the north-east quarter of its box is outside the outline.
    outline = (ring((0, 0), (0, 400), (200, 400), (200, 200), (400, 200), (400, 0)),)
    site = obj(
        "way/10", bounds=box(0, 0, 400, 400), kind="site", outline=outline, operator="Microsoft"
    )
    inside = obj("way/11", bounds=box(50, 50, 70, 70))  # unnamed, no operator
    campus = obj("way/12", bounds=box(250, 50, 350, 150), kind="campus", operator="Microsoft")
    notch = obj("way/13", bounds=box(300, 300, 320, 320))  # in the box, not in the outline
    other = obj("way/14", bounds=box(100, 300, 120, 320), operator="Amazon Web Services")
    clusters = dissolve([site, inside, campus, notch, other])
    assert groups(clusters) == {
        frozenset({"way/10", "way/11", "way/12"}),
        frozenset({"way/13"}),
        frozenset({"way/14"}),
    }
    (held,) = [c for c in clusters if "way/10" in c.refs]
    assert held.representative.ref == "way/10"  # the site is the largest area: its name leads
    # Without an outline the site's box + 30 m decides, as for a campus.
    boxed = obj("way/10", bounds=box(0, 0, 400, 400), kind="site", operator="Microsoft")
    assert frozenset({"way/10", "way/11", "way/12", "way/13"}) in groups(
        dissolve([boxed, inside, campus, notch, other])
    )


def test_point_in_outline_reads_multipolygon_members() -> None:
    # A relation's outer ring in two member ways, and an inner ring (a hole).
    a, b, c, d = offset(0, 0), offset(0, 100), offset(100, 100), offset(100, 0)
    outer = ((a, b, c), (c, d, a))
    hole = (ring((40, 40), (40, 60), (60, 60), (60, 40)),)
    assert point_in_outline(*offset(20, 20), outer)
    assert not point_in_outline(*offset(120, 20), outer)
    assert point_in_outline(*offset(20, 20), outer + hole)
    assert not point_in_outline(*offset(50, 50), outer + hole)


def test_the_same_specific_name_joins_within_500_m() -> None:
    """Seed check n13, n53, n69: QTS Data Center - Hillsboro 3's node had no operator and stayed
    apart from its two polygons; two Meta Henrico clusters 380 m apart and two Compugen New
    Albany buildings 350 m apart were separate records."""
    assert NAME_GAP_M == 500.0
    node = obj("node/1", at=(0, -100), name="QTS Data Center - Hillsboro 3")
    poly = obj(
        "way/2", bounds=box(0, 0, 50, 50), name="QTS Data Center - Hillsboro 3", operator="QTS"
    )
    far = obj("way/3", bounds=box(0, 430, 50, 480), name="qts data center hillsboro 3")  # 380 m
    gone = obj("way/4", bounds=box(0, 990, 50, 1040), name="QTS Data Center - Hillsboro 3")
    other = obj(
        "way/5",
        bounds=box(0, -900, 50, -850),
        name="QTS Data Center - Hillsboro 3",
        operator="Meta",
    )
    assert groups(dissolve([node, poly, far, gone, other])) == {
        frozenset({"node/1", "way/2", "way/3"}),
        frozenset({"way/4"}),  # 510 m from way/3
        frozenset({"way/5"}),  # operators disagree (and 750 m from the node)
    }
    # An object without an operator never bridges two operators' clusters of one name.
    near = obj(
        "way/5",
        bounds=box(0, -300, 50, -250),
        name="QTS Data Center - Hillsboro 3",
        operator="Meta",
    )
    found = groups(dissolve([node, poly, near]))
    assert len(found) == 2 and frozenset({"way/2", "way/5"}) not in found
    # A name that only names an operator, or only says "data center" or "building", is not
    # specific: it does not join across 500 m.
    words = frozenset({"google", "qts"})
    assert specific_name("Google", words) is None
    assert specific_name("QTS Data Center", words) is None
    assert specific_name("Building 2", words) is None
    assert specific_name("Meta Sarpy Data Center", words) == "meta sarpy data center"
    # Two objects of one such name and one operator join within 500 m all the same (second
    # seed check n4, n8: QTS Phoenix II's two "QTS" buildings 345 m apart); beyond, they do not.
    g1 = obj("way/6", bounds=box(500, 0, 550, 50), name="Google", operator="Google")
    g2 = obj("way/7", bounds=box(500, 400, 550, 450), name="Google", operator="Google")
    assert len(dissolve([g1, g2])) == 1
    g3 = obj("way/8", bounds=box(500, 951, 550, 1000), name="Google", operator="Google")
    assert len(dissolve([g1, g3])) == 2


def test_an_unnamed_object_joins_an_operator_at_its_address() -> None:
    """Seed check n61: an unnamed way without an operator at 8100 Boone Boulevard, 5 m from
    Digital Realty's node at the same address, was a record of its own."""
    node = obj(
        "node/1",
        at=(0, 0),
        name="8100 Boone Boulevard",
        operator="Digital Realty",
        addr__housenumber="8100",
        addr__street="Boone Boulevard",
    )
    way = obj(
        "way/2", bounds=box(5, 5, 60, 60), addr__housenumber="8100", addr__street="boone boulevard"
    )
    assert groups(dissolve([node, way])) == {frozenset({"node/1", "way/2"})}
    # A named one may be another tenant; and where operators at the address disagree (a carrier
    # hotel), nothing joins this way.
    named = obj(
        "way/2",
        bounds=box(5, 5, 60, 60),
        name="Annex",
        addr__housenumber="8100",
        addr__street="Boone Boulevard",
    )
    assert len(dissolve([node, named])) == 2
    tenant = obj(
        "node/3",
        at=(0, 80),
        name="Equinix",
        operator="Equinix",
        addr__housenumber="8100",
        addr__street="Boone Boulevard",
    )
    assert groups(dissolve([node, way, tenant])) == {
        frozenset({"node/1"}),
        frozenset({"way/2"}),
        frozenset({"node/3"}),
    }


def test_a_node_joins_the_building_it_lies_in() -> None:
    """Seed check n54: a data center node inside a building joins it, unless the operators of the
    building and of the nodes in it disagree; operator_conflicts lists those pairs for review."""
    hall = obj("way/1", bounds=box(0, 0, 100, 100))
    node = obj("node/2", at=(50, 50), name="Stream Data Centers Houston")
    assert groups(dissolve([hall, node])) == {frozenset({"way/1", "node/2"})}
    annex = obj("way/3", bounds=box(500, 0, 600, 100), name="USPO Terminal Annex", operator="USPS")
    coresite = obj("node/4", at=(550, 50), name="CoreSite - LA2", operator="CoreSite")
    assert len(dissolve([annex, coresite])) == 2
    assert [(a.ref, b.ref) for a, b in operator_conflicts([annex, coresite])] == [
        ("node/4", "way/3")
    ]
    # Two tenants of one building that disagree: neither joins it.
    hotel = obj("way/5", bounds=box(1000, 0, 1100, 100), name="Carrier Hotel")
    t1 = obj("node/6", at=(1020, 20), operator="Equinix")
    t2 = obj("node/7", at=(1080, 80), operator="Digital Realty")
    assert len(dissolve([hotel, t1, t2])) == 3
