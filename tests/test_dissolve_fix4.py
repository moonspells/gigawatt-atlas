"""The dissolve rules added after the second seed check of 2026-10-09 (n<number> is the finding in
the check's bug list): other primary uses and builders have no operator, operators a name implies,
sibling names, one name and one operator within 500 m, large unnamed halls, one-letter typos,
areas without an operator over two companies, an area's own address, and containment across a
county line. tests/sources/test_osm_fix4.py runs the importer on the real elements."""

from __future__ import annotations

from atlas.dissolve import (
    LARGE_HALL_GAP_M,
    LARGE_HALL_M2,
    Bounds,
    Cluster,
    DissolveNote,
    ObjectKind,
    OsmObject,
    dissolve,
    dissolve_with_notes,
    implied_operators,
    meters_to_degrees,
    one_edit,
    operator_forms,
    operators_compatible,
    ring_area_m2,
    sibling_stem,
)

LAT, LON = 39.0, -77.45


def offset(north_m: float, east_m: float) -> tuple[float, float]:
    dlat, dlon = meters_to_degrees(1.0, LAT)
    return LAT + north_m * dlat, LON + east_m * dlon


def box(south_m: float, west_m: float, north_m: float, east_m: float) -> Bounds:
    s, w = offset(south_m, west_m)
    n, e = offset(north_m, east_m)
    return Bounds(s, w, n, e)


def ring(*corners: tuple[float, float]) -> tuple[tuple[float, float], ...]:
    points = tuple(offset(*c) for c in corners)
    return (*points, points[0])


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


def test_another_primary_use_has_no_data_center_operator() -> None:
    """n0: the USPO Terminal Annex (amenity=post_office, operator United States Postal Service)
    holds CoreSite LA2's node. The post office's operator is not the data center's, so the node
    joins the building and the building names no operator."""
    annex = obj(
        "way/1",
        bounds=box(0, 0, 100, 100),
        name="USPO Terminal Annex",
        amenity="post_office",
        operator="United States Postal Service",
        operator__wikidata="Q668687",
        telecom="data_center",
    )
    la2 = obj("node/2", at=(50, 50), name="CoreSite - LA2", operator="CoreSite")
    assert annex.other_use == "amenity=post_office"
    assert (annex.operator, annex.operator_qid, annex.has_operator) == (None, None, False)
    assert groups(dissolve([annex, la2])) == {frozenset({"way/1", "node/2"})}
    # Without the other-use tag the operators disagree and the two stay apart (rule 6's guard).
    plain = obj("way/1", bounds=box(0, 0, 100, 100), operator="United States Postal Service")
    assert len(dissolve([plain, la2])) == 2


def test_a_general_contractor_is_not_the_operator() -> None:
    """n10, n14: a construction polygon tagged operator=HITT, the builder, became 'HITT data
    center' and kept Digital Realty IAD51 (at its own address) out. Its operator is not used; it
    holds the building at its address, not the AWS buildings its box also covers."""
    site = obj(
        "way/10",
        bounds=box(0, 0, 400, 400),
        kind="campus",
        operator="HITT",
        landuse="construction",
        construction="data_center",
        addr__housenumber="10051",
        addr__street="Brickyard Way",
    )
    iad51 = obj(
        "way/11",
        bounds=box(50, 50, 150, 150),
        operator="Digital Realty",
        addr__housenumber="10051",
        addr__street="Brickyard Way",
    )
    aws = [
        obj(f"way/{i}", bounds=box(300, x, 350, x + 40), operator="Amazon Web Services")
        for i, x in ((12, 200), (13, 250))
    ]
    assert site.builder == "HITT" and site.operator is None
    result = dissolve_with_notes([site, iad51, *aws])
    assert groups(result.clusters) == {
        frozenset({"way/10", "way/11"}),
        frozenset({"way/12", "way/13"}),
    }
    assert DissolveNote("mixed_operators", ("way/12", "way/10"), "1") in result.notes


def test_an_area_without_operator_holds_the_operator_most_of_its_contents_name() -> None:
    """The Digital Loudoun Plaza polygons have no operator and their box covers seven Digital
    Realty halls and two AWS buildings: before, all nine chained into one record."""
    plaza = obj("way/20", bounds=box(0, 0, 500, 500), kind="campus", name="Digital Loudoun Plaza")
    dlr = [
        obj(
            f"way/{21 + i}", bounds=box(20 + 60 * i, 20, 60 + 60 * i, 60), operator="Digital Realty"
        )
        for i in range(3)
    ]
    aws = obj("way/30", bounds=box(400, 400, 450, 450), operator="Amazon Web Services")
    assert groups(dissolve([plaza, *dlr, aws])) == {
        frozenset({"way/20", "way/21", "way/22", "way/23"}),
        frozenset({"way/30"}),
    }
    # Two operators with one building each: the area holds neither.
    tie = obj("way/31", bounds=box(400, 20, 450, 60), operator="Amazon Web Services")
    assert groups(dissolve([plaza, dlr[0], tie])) == {
        frozenset({"way/20"}),
        frozenset({"way/21"}),
        frozenset({"way/31"}),
    }


def test_operators_one_letter_apart_agree() -> None:
    """n14: the CyrusOne campus polygon is tagged operator=CyrysOne, a typo; its halls (operator
    CyrusOne) stayed a record of their own."""
    assert one_edit("cyrysone", "cyrusone") and one_edit("digital realty", "digital reality")
    assert not one_edit("vantage", "vantage") and not one_edit("qts", "qtx")  # short: no
    assert not one_edit("cyrusone", "corescale")
    campus = obj("way/40", bounds=box(0, 0, 400, 400), kind="campus", operator="CyrysOne")
    halls = [
        obj(
            f"way/{41 + i}",
            bounds=box(50 + 100 * i, 50, 120 + 100 * i, 150),
            operator="CyrusOne",
            operator__wikidata="Q16953751",
        )
        for i in range(3)
    ]
    assert groups(dissolve([campus, *halls])) == {
        frozenset({"way/40", "way/41", "way/42", "way/43"})
    }


def test_a_name_that_starts_with_an_operator_implies_it() -> None:
    """n33: 'Google Leesburg Building 3' (no operator) stood 180 m from Google's buildings; n6,
    n8: the 'Compass Data Center' halls have no operator, stand 54 m apart and named an operator
    word, so no rule joined them."""
    google = obj(
        "way/50", bounds=box(0, 0, 100, 100), name="Google Leesburg Building 1", operator="Google"
    )
    b3 = obj("way/51", bounds=box(0, 280, 100, 380), name="Google Leesburg Building 3")
    compass = obj("way/60", bounds=box(5000, 0, 5050, 50), operator="Compass Datacenters")
    halls = [
        obj(
            f"way/{61 + i}",
            bounds=box(0, 2000 + 104 * i, 50, 2050 + 104 * i),
            name="Compass Data Center",
        )
        for i in range(3)
    ]
    found = implied_operators([google, b3, compass, *halls])
    assert found == {
        "way/51": "google",
        "way/61": "compass",
        "way/62": "compass",
        "way/63": "compass",
    }
    clusters = dissolve([google, b3, compass, *halls])
    assert frozenset({"way/50", "way/51"}) in groups(clusters)
    assert frozenset({"way/61", "way/62", "way/63"}) in groups(clusters)
    # The clusters keep the objects as given: the implied operator only steers joins.
    (b3_out,) = [m for c in clusters for m in c.members if m.ref == "way/51"]
    assert b3_out.operator is None and b3_out.implied_operator is None
    # A name that two operators fit equally implies neither; generic or short words imply none.
    alpha = obj("way/70", at=(9000, 0), operator="Alpha Hosting", operator__short="Alpha")
    beta = obj("way/71", at=(9000, 900), operator="Alpha Networks", operator__short="Alpha")
    alone = obj("way/72", at=(9500, 0), name="Alpha East")
    it = obj("way/73", at=(9900, 0), operator="IT")
    it_room = obj("way/74", at=(9900, 900), name="IT Building")
    assert implied_operators([alpha, beta, alone, it, it_room]) == {}


def test_sibling_names_join_within_the_radius() -> None:
    """n2, n13, n36: KOMO Plaza East and West (40 m), EAT12 to EAT14 (109-226 m) and
    PowerHouse Pacific Building 1 to 3 (70-220 m, only Building 1 with an operator) were each a
    record of their own."""
    forms = operator_forms([obj("way/1", operator="PowerHouse Data Centers")])
    assert sibling_stem("KOMO Plaza West", forms) == "komo plaza"
    assert sibling_stem("EAT13", forms) == "eat"
    assert sibling_stem("PowerHouse Pacific Building 2", forms) == "powerhouse pacific building"
    assert sibling_stem("CyrusOne San Antonio IV", forms) == "cyrusone san antonio"
    for generic in (
        "Building 2",
        "Equinix DC10",
        "Data Center 1",
        "KOMO Plaza",
        "Plant A",
        "3433 South 120th Place",
    ):
        assert sibling_stem(generic, operator_forms([obj("way/2", operator="Equinix")])) is None
    east = obj("way/80", bounds=box(0, 0, 40, 60), name="KOMO Plaza East")
    west = obj("way/81", bounds=box(80, 0, 120, 60), name="KOMO Plaza West")
    b1 = obj(
        "way/82",
        bounds=box(1000, 0, 1100, 100),
        name="PowerHouse Pacific Building 1",
        operator="PowerHouse Data Centers",
    )
    b2 = obj("way/83", bounds=box(1000, 170, 1100, 270), name="PowerHouse Pacific Building 2")
    far = obj("way/84", bounds=box(1000, 571, 1100, 671), name="PowerHouse Pacific Building 9")
    other = obj(
        "way/85",
        bounds=box(1200, 0, 1300, 100),
        name="PowerHouse Pacific Building 4",
        operator="Digital Realty",
    )
    assert groups(dissolve([east, west, b1, b2, far, other])) == {
        frozenset({"way/80", "way/81"}),
        frozenset({"way/82", "way/83"}),
        frozenset({"way/84"}),  # 301 m from Building 2
        frozenset({"way/85"}),  # another operator
    }


def test_one_name_and_one_operator_join_within_500_m() -> None:
    """n8, n4: QTS Phoenix II's two buildings, both named 'QTS', stand 345 m apart, and Google
    Council Bluffs' expansion halls 478 m from the main ones; the name only names the operator,
    so rule 4 did not join them."""

    def qts(ref: str, bounds: Bounds) -> OsmObject:
        return obj(
            ref,
            bounds=bounds,
            name="QTS",
            operator="Quality Technology Services",
            operator__short="QTS",
        )

    a = qts("way/90", box(0, 0, 100, 100))
    b = qts("way/91", box(0, 445, 100, 545))
    c = qts("way/92", box(0, 1046, 100, 1146))  # 501 m from b
    d = obj("way/93", bounds=box(300, 0, 400, 100), name="QTS", operator="Meta")
    assert groups(dissolve([a, b, c, d])) == {
        frozenset({"way/90", "way/91"}),
        frozenset({"way/92"}),
        frozenset({"way/93"}),
    }


def test_large_unnamed_halls_join_within_100_m() -> None:
    """n6, n13: the three Stream San Antonio III halls (74 and 79 m apart), QTS Atlanta DC3 and
    DC4 (94 m) and a Prince William construction site's two buildings (99.7 m) have no name and
    no operator, so rule 3's 50 m kept each a record. Halls 104 m apart, and small buildings,
    stay apart."""
    assert (LARGE_HALL_M2, LARGE_HALL_GAP_M) == (5_000.0, 100.0)
    halls = [obj(f"way/{100 + i}", bounds=box(0, 194 * i, 100, 100 + 194 * i)) for i in range(2)]
    apart = obj("way/102", bounds=box(204, 0, 304, 100))  # 104 m north of way/100
    sheds = [obj(f"way/{103 + i}", bounds=box(2000, 90 * i, 2020, 20 + 90 * i)) for i in range(2)]
    assert groups(dissolve([*halls, apart, *sheds])) == {
        frozenset({"way/100", "way/101"}),
        frozenset({"way/102"}),
        frozenset({"way/103"}),
        frozenset({"way/104"}),
    }


def test_an_outline_holds_a_campus_across_a_county_line() -> None:
    """n40, n11: Microsoft TRP3 lies in the outline of the Texas Research Park campus but across
    the Bexar/Medina line, and became a campus of its own. Containment in an outline crosses the
    line (a note says so); a bounding box, and every other rule, still does not."""
    outline = (ring((0, 0), (0, 400), (400, 400), (400, 0)),)
    campus = obj(
        "way/110", bounds=box(0, 0, 400, 400), kind="campus", outline=outline, operator="Microsoft"
    )
    trp3 = obj("way/111", bounds=box(300, 300, 350, 350), operator="Microsoft")
    near = obj("way/112", bounds=box(0, 500, 50, 550), operator="Microsoft")  # 100 m east
    regions = {"way/110": "48029", "way/111": "48325", "way/112": "48325"}
    result = dissolve_with_notes([campus, trp3, near], regions=regions)
    assert groups(result.clusters) == {frozenset({"way/110", "way/111", "way/112"})}
    assert DissolveNote("county_line", ("way/111", "way/110"), "1") in result.notes
    boxed = obj("way/110", bounds=box(0, 0, 400, 400), kind="campus", operator="Microsoft")
    result = dissolve_with_notes([boxed, trp3], regions=regions)
    assert groups(result.clusters) == {frozenset({"way/110"}), frozenset({"way/111"})}
    assert result.notes == [DissolveNote("county_blocked", ("way/111", "way/110"), "1")]


def test_an_area_listing_a_buildings_address_with_another_operator_is_noted() -> None:
    """n3, n9: Microsoft's Quail Ridge Lane polygon lists the house numbers of the AWS buildings
    inside it; STACK's NVA04 polygon lists those of two AWS-tagged buildings. The operator guard
    keeps them apart, and a note says that the area names the building's address."""
    campus = obj(
        "way/120",
        bounds=box(0, 0, 400, 400),
        kind="campus",
        operator="Microsoft",
        addr__housenumber="24214;24224",
        addr__street="Quail Ridge Lane",
    )
    aws = obj(
        "way/121",
        bounds=box(100, 100, 200, 200),
        operator="Amazon Web Services",
        addr__housenumber="24224",
        addr__street="Quail Ridge Lane",
    )
    assert campus.addresses == {("24214", "quail ridge lane"), ("24224", "quail ridge lane")}
    assert not operators_compatible(aws, campus)
    result = dissolve_with_notes([campus, aws])
    assert groups(result.clusters) == {frozenset({"way/120"}), frozenset({"way/121"})}
    assert result.notes == [DissolveNote("address_conflict", ("way/121", "way/120"), "1")]


def test_ring_area() -> None:
    """n7: the area of a way's outline, to check a PNNL campus row against the polygon read."""
    square = (ring((0, 0), (0, 200), (200, 200), (200, 0)),)
    area = ring_area_m2(square)
    assert area is not None and abs(area - 40_000) < 400
    assert ring_area_m2(None) is None
    assert ring_area_m2((square[0][:3], square[0][2:])) is None  # a relation's member ways
