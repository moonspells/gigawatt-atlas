"""The dissolve rules of fix round 6, measured on the seed's inputs: a landlord's building leased
to a tenant inside an area without an operator (rule 1, owner_agrees), a building outside the
campus polygon of the row it is beside (rule 7), and two clusters of one operator that two
independent links tie (rule 9, multi_link_joins). tests/sources/test_osm_fix6.py runs the
importer on the real elements."""

from __future__ import annotations

from collections.abc import Mapping

from atlas.dissolve import (
    Bounds,
    Cluster,
    ObjectKind,
    OsmObject,
    dissolve_with_notes,
    meters_to_degrees,
    owner_agrees,
)

LAT, LON = 39.0, -77.47


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
    bounds: Bounds,
    tags: Mapping[str, str],
    kind: ObjectKind = "building",
    outline: tuple[tuple[tuple[float, float], ...], ...] | None = None,
) -> OsmObject:
    """An object; tags given as addr__street are addr:street."""
    found = {k.replace("__", ":"): v for k, v in tags.items()}
    lat, lon = bounds.center
    return OsmObject(ref, lat, lon, bounds, found, kind, outline)


def hall(ref: str, south_m: float, west_m: float, **tags: str) -> OsmObject:
    """A 60 m by 60 m data center building."""
    return obj(ref, box(south_m, west_m, south_m + 60, west_m + 60), tags)


def area(ref: str, south_m: float, west_m: float, side_m: float, **tags: str) -> OsmObject:
    """A campus polygon (no building tag) with its outline."""
    bounds = box(south_m, west_m, south_m + side_m, west_m + side_m)
    outline = square(south_m, west_m, side_m)
    return obj(ref, bounds, {"telecom": "data_center", **tags}, "campus", outline)


def groups(clusters: list[Cluster]) -> set[frozenset[str]]:
    return {frozenset(c.refs) for c in clusters}


# ---------------------------------------------------------------------------- rule 1


def plaza(**aws: str) -> list[OsmObject]:
    """The Digital Loudoun Plaza shape: a polygon without an operator (owner=Digital Realty) over
    two Digital Realty halls and an AWS hall, more than 300 m from each other."""
    return [
        area("way/1", 0, 0, 1000, owner="Digital Realty", landuse="industrial"),
        hall("way/2", 100, 100, operator="Digital Realty", owner="Digital Realty"),
        hall("way/3", 100, 500, operator="Digital Realty", owner="Digital Realty"),
        hall("way/4", 600, 300, operator="Amazon Web Services", **aws),
    ]


def test_a_landlords_building_leased_to_a_tenant_joins_the_landlords_site() -> None:
    """Amazon IAD-6 and IAD-13 (operator AWS, owner=Digital Realty) in the Digital Loudoun Plaza
    polygons were a record of their own beside the plaza's (owner's correction
    gwa-01m4hfqp14em6wf8wrm7ftzkvt)."""
    objects = plaza(owner="Digital Realty")
    assert owner_agrees(objects[3], objects[0], objects[1:3])
    result = dissolve_with_notes(objects)
    assert groups(result.clusters) == {frozenset({"way/1", "way/2", "way/3", "way/4"})}
    assert not [n for n in result.notes if n.kind == "mixed_operators"]


def test_a_tenants_building_without_the_landlords_owner_tag_stays_apart() -> None:
    for aws in ({}, {"owner": "Amazon"}):
        objects = plaza(**aws)
        assert not owner_agrees(objects[3], objects[0], objects[1:3])
        result = dissolve_with_notes(objects)
        assert frozenset({"way/4"}) in groups(result.clusters)
        (note,) = [n for n in result.notes if n.kind == "mixed_operators"]
        assert note.refs == ("way/4", "way/1")


# ---------------------------------------------------------------------------- rule 7


def test_a_building_outside_the_rows_site_polygon_does_not_join_it() -> None:
    """The former AOL building at 7777 Infantry Ridge Road (way 45583037, no name, no operator)
    is 39 m from Equinix DC14's building but outside Equinix's site polygon; it was a building
    of DC14 (owner's correction gwa-01m4hfqp7w390ge6zatq94ey6t)."""
    site = area("way/1", 0, 0, 200, operator="Equinix", landuse="industrial")
    dc14 = hall("way/2", 130, 130, operator="Equinix", name="Equinix DC14")
    unnamed = hall("way/3", 210, 130)  # 20 m north of DC14, outside the polygon
    assert groups(dissolve_with_notes([site, dc14, unnamed]).clusters) == {
        frozenset({"way/1", "way/2"}),
        frozenset({"way/3"}),
    }
    # Without a site polygon the row's rule stands (904 Quality Way beside Digital Dallas), and
    # a building inside the polygon still joins.
    assert groups(dissolve_with_notes([dc14, unnamed]).clusters) == {frozenset({"way/2", "way/3"})}
    inside = hall("way/4", 60, 130)  # 10 m south of DC14, inside the polygon
    assert frozenset({"way/1", "way/2", "way/4"}) in groups(
        dissolve_with_notes([site, dc14, inside]).clusters
    )


# ---------------------------------------------------------------------------- rule 9


def lc_rows(
    more: Mapping[str, str] | None = None, second: Mapping[str, str] | None = None
) -> list[OsmObject]:
    """CloudHQ's LC campus shape: LC1 to LC3 and LC4 to LC6, 600 m apart (beyond rule 8's
    500 m), each a row rule 2 joins; more gives every hall more tags, second LC4 to LC6."""
    return [
        obj(
            f"way/{n}",
            box(0, west, 60, west + 60),
            {
                "operator": "CloudHQ",
                "name": f"CloudHQ LC{n}",
                **(more or {}),
                **((second or {}) if n > 3 else {}),
            },
        )
        for n, west in ((1, 0), (2, 100), (3, 200), (4, 860), (5, 960), (6, 1060))
    ]


STREET = {"addr__street": "Loudoun County Parkway"}
PAGE = {"website": "https://cloudhq.com/campus/lc-campus/"}
ROWS = {frozenset({"way/1", "way/2", "way/3"}), frozenset({"way/4", "way/5", "way/6"})}


def test_two_links_join_one_operators_clusters() -> None:
    """CloudHQ LC1 to LC3 and LC4 to LC14 (owner's correction gwa-01m4hfqpdz4tepn3b5d6473r4n):
    the series LC3-LC4, Loudoun County Parkway and CloudHQ's LC campus page tie them."""
    for links in ({**STREET}, {**PAGE}, {**STREET, **PAGE}):
        result = dissolve_with_notes(lc_rows(links))
        assert groups(result.clusters) == {frozenset(f"way/{n}" for n in range(1, 7))}, links
    # Another operator's building between them does not keep two links apart (STACK NVA04C's
    # node lies between CloudHQ's two rows).
    between = hall("way/9", 0, 530, operator="STACK Infrastructure", name="STACK NVA04C")
    result = dissolve_with_notes([*lc_rows(STREET | PAGE), between])
    assert frozenset(f"way/{n}" for n in range(1, 7)) in groups(result.clusters)


def test_one_link_or_a_separation_keeps_the_clusters_apart() -> None:
    """A series alone is a note, not a join (RagingWire CA3, 720 m from CA2), and so are two
    links when the ZIP codes differ."""
    zips = lc_rows(STREET | PAGE | {"addr__postcode": "20147"}, {"addr__postcode": "20148"})
    for objects in (lc_rows(), zips):
        result = dissolve_with_notes(objects)
        assert groups(result.clusters) == ROWS
        assert [n.kind for n in result.notes] == ["same_operator_nearby"]
