"""Dissolve OpenStreetMap data center objects into campuses (07 §4.2 step 2).

The Overpass query returns campuses (landuse-style polygons tagged telecom=data_center without a
building tag), buildings and points. One campus record should stand for each site, so objects are
joined with union-find:

1. Containment: an object whose center lies inside a campus object's bounding box, expanded by
   30 m, joins that campus; inside several, it joins the smallest one. A campus and an object
   whose operators are both known and differ (neither the normalized operator nor
   operator:wikidata agree) do not join this way, because a bounding box is only an
   approximation of the campus polygon and in dense areas such as Ashburn it covers other
   operators' buildings.
2. Operator radius: two non-campus objects whose centers are within radius_m (haversine) join when
   they have the same normalize_org(operator) or the same operator:wikidata.

Objects without an operator join only through rule 1, and two campus objects never join each other
directly. The result does not depend on input order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from atlas.text import normalize_org

ObjectKind = Literal["campus", "building", "point"]

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_DEGREE_LAT = math.pi * EARTH_RADIUS_M / 180  # about 111,195 m, the same sphere
CONTAIN_MARGIN_M = 30.0
DEFAULT_RADIUS_M = 300.0
_TYPE_ORDER = {"node": 0, "way": 1, "relation": 2}


def ref_key(ref: str) -> tuple[int, int, str]:
    """Sort key for "node/1" | "way/1" | "relation/1": nodes, then ways, then relations, by id."""
    osm_type, _, osm_id = ref.partition("/")
    return (_TYPE_ORDER.get(osm_type, 9), int(osm_id) if osm_id.isdigit() else -1, ref)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def meters_to_degrees(meters: float, lat: float) -> tuple[float, float]:
    """(dlat, dlon) in degrees for a distance in metres at a latitude."""
    dlat = meters / METERS_PER_DEGREE_LAT
    dlon = meters / (METERS_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 1e-6))
    return dlat, dlon


@dataclass(frozen=True)
class Bounds:
    """An Overpass `bb` bounding box in degrees."""

    minlat: float
    minlon: float
    maxlat: float
    maxlon: float

    @property
    def center(self) -> tuple[float, float]:
        """The midpoint (what Overpass `out center` returns for a way or relation)."""
        return (self.minlat + self.maxlat) / 2, (self.minlon + self.maxlon) / 2

    def area_m2(self) -> float:
        """Approximate area in square metres (equirectangular)."""
        lat = (self.minlat + self.maxlat) / 2
        height = (self.maxlat - self.minlat) * METERS_PER_DEGREE_LAT
        width = (self.maxlon - self.minlon) * METERS_PER_DEGREE_LAT * math.cos(math.radians(lat))
        return abs(height * width)

    def expanded(self, margin_m: float) -> Bounds:
        dlat, dlon = meters_to_degrees(margin_m, (self.minlat + self.maxlat) / 2)
        return Bounds(
            self.minlat - dlat, self.minlon - dlon, self.maxlat + dlat, self.maxlon + dlon
        )

    def contains(self, lat: float, lon: float) -> bool:
        return self.minlat <= lat <= self.maxlat and self.minlon <= lon <= self.maxlon


@dataclass(frozen=True)
class OsmObject:
    """One Overpass element. lat/lon is the node position or the bounding-box midpoint."""

    ref: str  # "node/1" | "way/1" | "relation/1"
    lat: float
    lon: float
    bounds: Bounds | None
    tags: Mapping[str, str] = field(hash=False)
    kind: ObjectKind

    @property
    def osm_type(self) -> str:
        return self.ref.partition("/")[0]

    @property
    def osm_id(self) -> int:
        return int(self.ref.partition("/")[2])

    @property
    def name(self) -> str | None:
        return self.tags.get("name") or None

    @property
    def operator(self) -> str | None:
        return self.tags.get("operator") or None

    @property
    def operator_key(self) -> str | None:
        """normalize_org(operator), or None when there is no usable operator."""
        op = self.operator
        key = normalize_org(op) if op else ""
        return key or None

    @property
    def operator_qid(self) -> str | None:
        return self.tags.get("operator:wikidata") or None

    def area_m2(self) -> float:
        return self.bounds.area_m2() if self.bounds is not None else 0.0


@dataclass(frozen=True)
class Cluster:
    """A dissolved site. members are sorted by ref_key; representative is one of them."""

    members: tuple[OsmObject, ...]
    representative: OsmObject

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(m.ref for m in self.members)

    @property
    def campuses(self) -> tuple[OsmObject, ...]:
        return tuple(m for m in self.members if m.kind == "campus")


def same_operator(a: OsmObject, b: OsmObject) -> bool:
    """Same normalized operator name, or same operator:wikidata."""
    ka, kb = a.operator_key, b.operator_key
    if ka is not None and ka == kb:
        return True
    qa, qb = a.operator_qid, b.operator_qid
    return qa is not None and qa == qb


def operators_compatible(obj: OsmObject, campus: OsmObject) -> bool:
    """False only when both carry operator information and it disagrees."""
    obj_known = obj.operator_key is not None or obj.operator_qid is not None
    campus_known = campus.operator_key is not None or campus.operator_qid is not None
    if not obj_known or not campus_known:
        return True
    return same_operator(obj, campus)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def containing_campus(
    obj: OsmObject, campuses: Sequence[OsmObject], *, margin_m: float = CONTAIN_MARGIN_M
) -> OsmObject | None:
    """The smallest compatible campus whose expanded bounding box holds obj's center (rule 1)."""
    hits = [
        c
        for c in campuses
        if c.ref != obj.ref
        and c.bounds is not None
        and c.bounds.expanded(margin_m).contains(obj.lat, obj.lon)
        and operators_compatible(obj, c)
    ]
    if not hits:
        return None
    return min(hits, key=lambda c: (c.area_m2(), ref_key(c.ref)))


def choose_representative(members: Sequence[OsmObject]) -> OsmObject:
    """The campus object (the largest if several), else the largest way or relation by bounding
    box, else the node with the lowest id."""
    campuses = [m for m in members if m.kind == "campus"]
    if campuses:
        return min(campuses, key=lambda m: (-m.area_m2(), ref_key(m.ref)))
    shapes = [m for m in members if m.osm_type in ("way", "relation")]
    if shapes:
        return min(shapes, key=lambda m: (-m.area_m2(), ref_key(m.ref)))
    return min(members, key=lambda m: ref_key(m.ref))


def dissolve(objects: Sequence[OsmObject], *, radius_m: float = DEFAULT_RADIUS_M) -> list[Cluster]:
    """Group objects into clusters (see the module docstring). Clusters are sorted by their
    representative's ref_key, and each cluster's members by ref_key."""
    if radius_m <= 0:
        raise ValueError(f"radius_m must be positive, not {radius_m}")
    by_ref: dict[str, OsmObject] = {}
    for obj in objects:
        if obj.ref in by_ref:
            raise ValueError(f"duplicate OSM object {obj.ref}")
        by_ref[obj.ref] = obj
    ordered = sorted(by_ref.values(), key=lambda o: ref_key(o.ref))
    index = {o.ref: i for i, o in enumerate(ordered)}
    uf = _UnionFind(len(ordered))

    campuses = [o for o in ordered if o.kind == "campus"]
    others = [o for o in ordered if o.kind != "campus"]

    # Rule 1: containment in a campus bounding box.
    for obj in others:
        campus = containing_campus(obj, campuses)
        if campus is not None:
            uf.union(index[obj.ref], index[campus.ref])

    # Rule 2: same operator (name or Wikidata id) within radius_m.
    groups: dict[str, list[OsmObject]] = {}
    for obj in others:
        if obj.operator_key is not None:
            groups.setdefault("name:" + obj.operator_key, []).append(obj)
        if obj.operator_qid is not None:
            groups.setdefault("qid:" + obj.operator_qid, []).append(obj)
    for key in sorted(groups):
        members = groups[key]
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if haversine_m(a.lat, a.lon, b.lat, b.lon) <= radius_m:
                    uf.union(index[a.ref], index[b.ref])

    grouped: dict[int, list[OsmObject]] = {}
    for obj in ordered:
        grouped.setdefault(uf.find(index[obj.ref]), []).append(obj)
    clusters = [
        Cluster(members=tuple(members), representative=choose_representative(members))
        for members in grouped.values()
    ]
    return sorted(clusters, key=lambda c: ref_key(c.representative.ref))
