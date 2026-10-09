"""The PNNL cross-check after the pre-check of the fourth seed import (fix round 5, n13): a matched
row whose name gives the member another building number is a conflict item.
tests/sources/test_osm_fix5.py runs it on the real NTT Ashburn row."""

from __future__ import annotations

import pytest

from atlas.dissolve import Bounds, Cluster, OsmObject
from atlas.sources.pnnl import PnnlRow, join, site_number, site_values


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("NTT Ashburn VA8 Data Centre", ("va", 8)),
        ("NTT VA9", ("va", 9)),
        ("Amazon IAD-78", ("iad", 78)),
        ("Amazon IAD78", ("iad", 78)),
        ("PowerHouse Pacific Building 3", ("building", 3)),
        ("Google Data Center", None),
        (None, None),
    ],
)
def test_site_number(name: str | None, expected: tuple[str, int] | None) -> None:
    assert site_number(name) == expected


def building(name: str) -> OsmObject:
    bounds = Bounds(39.0195, -77.4765, 39.0206, -77.4745)
    lat, lon = bounds.center
    return OsmObject("way/1", lat, lon, bounds, {"name": name, "building": "yes"}, "building")


@pytest.mark.parametrize(
    ("osm_name", "row_name", "conflict"),
    [
        ("NTT VA9", "NTT Ashburn VA8 Data Centre", True),
        ("Amazon IAD-61", "Amazon IAD-63", True),
        ("Amazon IAD-78", "Amazon IAD78", False),  # punctuation only
        ("NTT VA9", "NTT Ashburn Data Centre", False),  # no number in the row
        ("CyrusOne NVA9", "CyrusOne Sterling IX", False),  # no code of the same kind
    ],
)
def test_another_building_number_is_a_conflict(
    osm_name: str, row_name: str, conflict: bool
) -> None:
    member = building(osm_name)
    cluster = Cluster(members=(member,), representative=member)
    row = PnnlRow(type="building", lat=member.lat, lon=member.lon, name=row_name, sqft=1000.0)
    result = join([cluster], [row])
    values = site_values(cluster, result.matched["way/1"], result.members)
    found = [i for i in values.review if i.kind == "conflict"]
    assert bool(found) == conflict
    if conflict:
        assert found[0].external_id == row.key and found[0].data["osm"] == "way/1"
        assert repr(row_name) in found[0].reason and repr(osm_name) in found[0].reason
