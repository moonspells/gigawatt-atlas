"""The dissolve rules added after the pre-check of the fourth seed import (fix round 5;
n<number> is the finding in its bug list): adjoining areas of one operator (rule 2), an unnamed
building in one operator's row (rule 7), a numbered sibling or a street neighbour just beyond the
radius (rule 8), and the notes for the splits no rule joins. tests/sources/test_osm_fix5.py runs
the importer on the real elements."""

from __future__ import annotations

from atlas.dissolve import (
    ADJOIN_M,
    DEFAULT_RADIUS_M,
    NAME_GAP_M,
    Bounds,
    Cluster,
    DissolveNote,
    ObjectKind,
    OsmObject,
    adjoining_gap_m,
    choose_representative,
    dissolve_with_notes,
    meters_to_degrees,
    operator_forms,
    outline_gap_m,
    series_key,
)

LAT, LON = 38.77, -77.54


def offset(north_m: float, east_m: float) -> tuple[float, float]:
    dlat, dlon = meters_to_degrees(1.0, LAT)
    return LAT + north_m * dlat, LON + east_m * dlon


def box(south_m: float, west_m: float, north_m: float, east_m: float) -> Bounds:
    s, w = offset(south_m, west_m)
    n, e = offset(north_m, east_m)
    return Bounds(s, w, n, e)


def square(south_m: float, west_m: float, side_m: float) -> tuple[tuple[tuple[float, float], ...]]:
    corners = [
        (south_m, west_m),
        (south_m, west_m + side_m),
        (south_m + side_m, west_m + side_m),
        (south_m + side_m, west_m),
    ]
    points = tuple(offset(*c) for c in corners)
    return ((*points, points[0]),)


def obj(
    ref: str,
    *,
    at: tuple[float, float] = (0.0, 0.0),
    bounds: Bounds | None = None,
    kind: ObjectKind | None = None,
    outline: tuple[tuple[tuple[float, float], ...], ...] | None = None,
    tags: dict[str, str] | None = None,
    **more: str,
) -> OsmObject:
    """An object; tags and more ("addr__street" for "addr:street") are its tags."""
    lat, lon = bounds.center if bounds is not None else offset(*at)
    if kind is None:
        kind = "point" if ref.startswith("node/") else "building"
    found = {k.replace("__", ":"): v for k, v in {**(tags or {}), **more}.items()}
    return OsmObject(ref, lat, lon, bounds, found, kind, outline)


def site(ref: str, south_m: float, west_m: float, side_m: float, **tags: str) -> OsmObject:
    return obj(
        ref,
        bounds=box(south_m, west_m, south_m + side_m, west_m + side_m),
        kind="site",
        outline=square(south_m, west_m, side_m),
        tags={"industrial": "data_centre", "landuse": "industrial", **tags},
    )


def groups(clusters: list[Cluster]) -> set[frozenset[str]]:
    return {frozenset(c.refs) for c in clusters}


def hall(ref: str, south_m: float, west_m: float, **tags: str) -> OsmObject:
    """A 60 m by 60 m building."""
    return obj(ref, bounds=box(south_m, west_m, south_m + 60, west_m + 60), tags=tags)


# ---------------------------------------------------------------------------- rule 2, areas


def test_adjoining_sites_of_one_operator_join() -> None:
    """n7, n19: Meta New Albany and its LCO 3 expansion site touch; Google Lenoir's two outers
    are 108 m apart. Each was two records."""
    campus = site("way/1", 0, 0, 400, name="Meta New Albany Data Center", operator="Meta")
    expansion = site(
        "way/2", 0, 500, 400, name="Meta LCO 3 Project", operator="Meta", landuse="construction"
    )
    assert adjoining_gap_m(campus, expansion) is not None
    result = dissolve_with_notes([campus, expansion])
    assert groups(result.clusters) == {frozenset({"way/1", "way/2"})}
    # The expansion site under construction does not lead the campus, although it is larger.
    bigger = site("way/3", 0, 500, 600, operator="Meta", landuse="construction")
    assert choose_representative([campus, bigger]).ref == "way/1"
    # Beyond ADJOIN_M the sites stay apart.
    far = site("way/4", 0, 400 + ADJOIN_M + 10, 400, name="Meta Elsewhere", operator="Meta")
    assert len(dissolve_with_notes([campus, far]).clusters) == 2
    # Another operator's adjoining site does not join.
    other = site("way/5", 0, 450, 400, name="Google Data Center", operator="Google")
    assert len(dissolve_with_notes([campus, other]).clusters) == 2


def test_sites_with_different_addresses_are_a_note() -> None:
    """Microsoft's Project Alluvion and Project Ginger East (West Des Moines) are 126 m apart and
    list different street addresses: two projects of one operator, a note, not a join."""
    a = site(
        "way/1",
        0,
        0,
        400,
        name="Project Alluvion",
        operator="Microsoft",
        addr__housenumber="550",
        addr__street="Southeast White Crane Road",
    )
    b = site(
        "way/2",
        0,
        500,
        400,
        name="Project Ginger East",
        operator="Microsoft",
        addr__housenumber="1475",
        addr__street="Southeast Maffitt Lake Road",
    )
    result = dissolve_with_notes([a, b])
    assert len(result.clusters) == 2
    (note,) = result.notes
    assert (note.kind, note.refs, note.rule) == ("same_operator_nearby", ("way/1", "way/2"), "2")
    assert "different street addresses" in note.why


def test_areas_without_outlines_join_only_when_their_boxes_overlap() -> None:
    a = obj("way/1", bounds=box(0, 0, 100, 100), kind="campus", operator="Google")
    near = obj("way/2", bounds=box(0, 150, 100, 250), kind="campus", operator="Google")
    over = obj("way/3", bounds=box(50, 50, 150, 150), kind="campus", operator="Google")
    assert adjoining_gap_m(a, near) is None
    assert adjoining_gap_m(a, over) == 0.0
    assert groups(dissolve_with_notes([a, near]).clusters) == {
        frozenset({"way/1"}),
        frozenset({"way/2"}),
    }
    assert groups(dissolve_with_notes([a, over]).clusters) == {frozenset({"way/1", "way/3"})}


def test_outline_gap() -> None:
    assert outline_gap_m(square(0, 0, 100), square(0, 150, 100)) == pytest_approx(50.0)
    assert outline_gap_m(square(0, 0, 100), square(50, 50, 100)) == 0.0


def pytest_approx(value: float) -> object:
    import pytest

    return pytest.approx(value, abs=0.5)


# ---------------------------------------------------------------------------- rule 7


def test_an_unnamed_building_in_one_operators_row_joins_it() -> None:
    """n9: 904 Quality Way (no name, no operator) is 18 m from Digital Realty DFW18."""
    row = [
        hall(
            f"way/{i}", 0, 100 * i, name=f"Digital Realty Dallas DFW{i}", operator="Digital Realty"
        )
        for i in (1, 2, 3)
    ]
    unnamed = hall("way/9", 78, 100)  # 18 m north of DFW1
    assert groups(dissolve_with_notes([*row, unnamed]).clusters) == {
        frozenset({"way/1", "way/2", "way/3", "way/9"})
    }
    # Another operator within the radius: it may be that operator's, so it stays apart.
    rackspace = hall("way/8", 200, 100, name="Rackspace DFW3", operator="Rackspace")
    result = dissolve_with_notes([*row, unnamed, rackspace])
    assert frozenset({"way/9"}) in groups(result.clusters)
    # A node marks a tenant, not a row; and a way drawn over a building is a duplicate.
    node = obj("node/7", at=(150, 130), operator="Digital Realty")  # 12 m north of way/9
    assert frozenset({"way/9"}) in groups(dissolve_with_notes([node, unnamed]).clusters)
    twin = obj("way/10", bounds=box(5, 105, 55, 155))
    assert frozenset({"way/10"}) in groups(dissolve_with_notes([*row, twin]).clusters)


def test_a_lone_building_beside_another_operators_row_is_a_note() -> None:
    """n9: the Rackspace-tagged building 42 m from Digital Realty's six Digital Dallas buildings
    is DFW25 of that campus by Digital Realty's own list."""
    row = [
        hall(f"way/{i}", 0, 100 * i, name=f"DFW{i}", operator="Digital Realty") for i in (1, 2, 3)
    ]
    lone = hall("way/8", 102, 100, name="Rackspace Richardson DFW3", operator="Rackspace")
    result = dissolve_with_notes([*row, lone])
    (note,) = [n for n in result.notes if n.kind == "tenant_building"]
    assert note.refs == ("way/8", "way/1")
    assert "operator 'Rackspace'" in note.why and "3 buildings of 'Digital Realty'" in note.why
    # A third operator around it: a mixed area, no note.
    third = hall("way/20", 102, 300, name="Equinix X1", operator="Equinix")
    result = dissolve_with_notes([*row, lone, third])
    assert not [n for n in result.notes if n.kind == "tenant_building"]


# ---------------------------------------------------------------------------- rule 8


def iron(i: int, east_m: float, postcode: str = "20109") -> OsmObject:
    return hall(
        f"way/{i}",
        0,
        east_m,
        name=f"Iron Mountain VA-{i}",
        operator="Iron Mountain",
        operator__wikidata="Q1673079",
        addr__postcode=postcode,
    )


def test_a_numbered_sibling_just_beyond_the_radius_joins() -> None:
    """n4: Iron Mountain VA-6 is 364 m from VA-5, one campus of seven buildings."""
    five, six = iron(5, 0), iron(6, 60 + 364)
    assert DEFAULT_RADIUS_M < five.gap_m(six) <= NAME_GAP_M
    result = dissolve_with_notes([five, six])
    assert groups(result.clusters) == {frozenset({"way/5", "way/6"})}
    # Two numbers apart is no sibling; nor is a building of another series.
    eight = iron(8, 60 + 364)
    assert len(dissolve_with_notes([five, eight]).clusters) == 2


def test_a_same_street_building_just_beyond_the_radius_joins() -> None:
    """n18: Digital Realty IAD41 is 348 m from IAD39 on Round Table Plaza."""
    tags = {"operator": "Digital Realty", "addr__street": "Round Table Plaza"}
    a = hall("way/1", 0, 0, name="Digital Realty IAD39", **tags)
    b = hall("way/2", 0, 60 + 348, name="Digital Realty IAD41", **tags)
    assert groups(dissolve_with_notes([a, b]).clusters) == {frozenset({"way/1", "way/2"})}


def test_what_separates_a_sibling_is_a_note() -> None:
    five = iron(5, 0)
    cases = {
        "their postcodes differ (20109 and 20110)": [iron(6, 424, postcode="20110")],
        "of another operator lies between them": [
            iron(6, 424),
            hall("way/30", 0, 200, name="Equinix DC1", operator="Equinix"),
        ],
        "each lies in its own site polygon": [
            iron(6, 424),
            site("way/40", -10, -10, 80, operator="Iron Mountain"),
            site("way/41", -10, 414, 80, operator="Iron Mountain"),
        ],
    }
    for why, more in cases.items():
        result = dissolve_with_notes([five, *more])
        assert "way/6" not in next(c for c in result.clusters if "way/5" in c.refs).refs, why
        (note,) = [n for n in result.notes if n.kind == "same_operator_nearby"]
        assert note.rule == "8" and why in note.why, note
    points = [
        obj("node/1", at=(0, 0), name="Digital Realty JFK12", operator="Digital Realty"),
        obj("node/2", at=(0, 419), name="Digital Realty JFK13", operator="Digital Realty"),
    ]
    result = dissolve_with_notes(points)
    assert len(result.clusters) == 2
    (note,) = result.notes
    assert "both are points" in note.why


def test_a_series_split_further_apart_is_a_note() -> None:
    """n8, n17: RagingWire CA3 is 720 m from CA2; CloudHQ's LC4 group 650 m from LC1 to LC3."""
    tags = {"operator": "NTT", "operator__wikidata": "Q1054787"}
    ca2 = hall("way/2", 0, 0, name="RagingWire CA2", **tags)
    ca3 = hall("way/3", 0, 60 + 720, name="RagingWire CA3", **tags)
    result = dissolve_with_notes([ca2, ca3])
    assert len(result.clusters) == 2
    assert result.notes == [
        DissolveNote(
            "same_operator_nearby",
            ("way/2", "way/3"),
            "",
            "'RagingWire CA3' continues the series of 'RagingWire CA2', 720 m apart",
        )
    ]
    # One web page names both, within SITE_PAGE_GAP_M.
    page = "https://cloudhq.com/campus/lc-campus/"
    lc1 = hall("way/11", 0, 0, name="CloudHQ LC1", operator="CloudHQ", website=page)
    lc8 = hall("way/18", 0, 60 + 1500, name="CloudHQ LC8", operator="CloudHQ", website=page)
    (note,) = dissolve_with_notes([lc1, lc8]).notes
    assert "both give the web page" in note.why


def test_series_key() -> None:
    forms = operator_forms([obj("way/1", operator="Iron Mountain"), obj("way/2", operator="QTS")])
    assert series_key("Iron Mountain VA-6", forms) == ("iron mountain va", 6)
    assert series_key("Digital Realty IAD41", forms) == ("digital realty iad", 41)
    assert series_key("QTS NAL1 DC2", forms) == ("qts nal1 dc", 2)
    assert series_key("QTS NAL 2 DC1", forms) == ("qts nal 2 dc", 1)
    assert series_key("Building 2", forms) is None
    assert series_key("Flexential Hillsboro 4 Expansion", forms) is None
