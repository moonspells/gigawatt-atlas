"""Regression tests for what the 2026-10-08 seed check found in the PNNL join (n<number> is the
finding)."""

from __future__ import annotations

from atlas.dissolve import Bounds, Cluster, ObjectKind, OsmObject, dissolve
from atlas.sources import pnnl
from atlas.sources.pnnl import PnnlRow


def area(ref: str, kind: ObjectKind, lat: float, size: float, **tags: str) -> OsmObject:
    bounds = Bounds(lat, -77.5, lat + size, -77.5 + size)
    return OsmObject(ref, *bounds.center, bounds, tags, kind)


def test_no_building_sqft_and_no_partial_acreage() -> None:
    """n3, n5, n81: PNNL's sqft is the footprint polygon's area, and the sum of the matched
    buildings was published as site.building_sqft (a partial one where some buildings had no
    row). Building rows now set nothing; acreage needs a campus row on every campus or site
    member that no other one covers, and a nested campus does not add to its site."""
    site = area("way/1", "site", 39.0, 0.01, name="Example Campus")
    nested = area("way/2", "campus", 39.002, 0.002)  # inside the site
    other = area("way/3", "campus", 39.02, 0.002)  # a second campus of the cluster, apart
    hall = area("way/4", "building", 39.003, 0.0005)

    def row(m: OsmObject, typ: str, sqft: float) -> PnnlRow:
        return PnnlRow(type=typ, lat=m.lat, lon=m.lon, sqft=sqft)

    rows = [
        row(site, "campus", 435_600.0),
        row(nested, "campus", 43_560.0),
        row(hall, "building", 9.0),
    ]
    members = dict(zip((r.key for r in rows), ("way/1", "way/2", "way/4"), strict=True))
    whole = Cluster(members=(site, nested, hall), representative=site)
    values = pnnl.site_values(whole, rows, members)
    assert values.acreage == 10.0  # the site's row; the nested campus's acre is inside it
    assert not hasattr(values, "site_sqft") and not hasattr(values, "building_sqft")
    partial = Cluster(members=(site, nested, other, hall), representative=site)
    assert pnnl.site_values(partial, rows, members).acreage is None  # way/3 has no campus row


def test_containment_comes_before_the_name() -> None:
    """n4: the row "QTS Manassas DC1" lay 6 m from the center of the polygon OSM now calls DC2,
    and within DC1's box + 30 m; the name won, and the row went to DC1."""
    lat, lon = 38.75, -77.51
    d = 0.0004
    dc1 = OsmObject(
        "way/1",
        lat,
        lon - 0.0007,
        Bounds(lat - d, lon - 0.0007 - d, lat + d, lon - 0.0007 + d),
        {"name": "QTS Manassas DC1"},
        "building",
    )
    dc2 = OsmObject(
        "way/2",
        lat,
        lon,
        Bounds(lat - d, lon - d, lat + d, lon + d),
        {"name": "QTS Manassas DC2"},
        "building",
    )
    stale = PnnlRow(type="building", lat=lat, lon=lon - 0.00006, name="QTS Manassas DC1")
    assert dc1.bounds is not None and dc1.bounds.expanded(30).contains(stale.lat, stale.lon)
    assert pnnl.spatial_match(stale, [dc1, dc2]) is dc2
    # Where boxes overlap, a campus row goes to the campus and a building row to the building.
    campus = OsmObject(
        "way/3", lat, lon, Bounds(lat - 0.01, lon - 0.01, lat + 0.01, lon + 0.01), {}, "campus"
    )
    assert pnnl.spatial_match(PnnlRow(type="campus", lat=lat, lon=lon), [dc2, campus]) is campus
    assert pnnl.spatial_match(PnnlRow(type="building", lat=lat, lon=lon), [dc2, campus]) is dc2


def test_a_moved_node_matches_by_name() -> None:
    """n45: the PNNL point is OSM node 13311012216's old position; the node moved 119 m. A row
    that reaches no member matches the nearest member of its name within 250 m, unless the
    operators disagree."""
    node = OsmObject(
        "node/13311012216",
        36.065929,
        -115.13856,
        None,
        {"name": "Fiberhub LAS1", "operator": "Fiberhub"},
        "point",
    )
    row = PnnlRow(
        type="point", lat=36.0659405, lon=-115.1398835, name="Fiberhub LAS1", operator="Fiberhub"
    )
    result = pnnl.join(dissolve([node]), [row])
    assert result.matched == {"node/13311012216": [row]} and result.by_name == 1
    other = PnnlRow(
        type="point", lat=row.lat, lon=row.lon, name="Fiberhub LAS1", operator="Equinix"
    )
    far = PnnlRow(type="point", lat=36.069, lon=row.lon, name="Fiberhub LAS1")  # 362 m
    result = pnnl.join(dissolve([node]), [other, far])
    assert result.unmatched == [other, far] and result.by_name == 0
    assert result.nearest[far.key] == ("node/13311012216", 361.6)
    item = pnnl.unmatched_item(far, result.nearest[far.key])
    assert item.reason.endswith("the nearest is node/13311012216, 362 m away")
    assert "PNNL point:" in item.reason and "point point" not in item.reason
