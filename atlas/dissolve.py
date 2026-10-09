"""Dissolve OpenStreetMap data center objects into campuses (07 §4.2 step 2).

The Overpass query returns campuses (landuse-style polygons tagged as a data center without a
building tag), sites (polygons tagged industrial=data_centre and nothing else that marks a data
center), buildings and points. One campus record should stand for each site, so objects are
joined with union-find:

1. Containment: an object whose center lies inside a campus object's bounding box, expanded by
   30 m, joins that campus; inside several, it joins the smallest one. Any object (a campus too)
   whose center lies inside a site polygon (its outline when the query returned one, else its
   bounding box expanded by 30 m) joins the smallest such site. A campus or site and an object
   whose operators disagree do not join this way (operators_compatible), because a bounding box
   is only an approximation of the campus polygon and in dense areas such as Ashburn it covers
   other operators' buildings.
2. Operator distance: two non-campus objects whose bounding boxes (a node is its point) are within
   radius_m of each other join when they have the same normalize_org(operator) or the same
   operator:wikidata.
3. Neighbours without an operator: two non-campus objects that both have no operator information
   join when their bounding boxes are within NEIGHBOR_GAP_M and they have the same
   normalize_name(name) or neither has a name, or when they have the same addr:housenumber and
   addr:street and their boxes are within radius_m.
4. Same name: two objects of any kind with the same specific normalize_name(name) (one that says
   more than an operator and words such as "data center" or "building", see specific_name) join
   when their boxes are within NAME_GAP_M and the operators of their two clusters are compatible,
   so an operator-less node joins the polygons of the same name but never bridges two operators.
5. Same address: an unnamed object without operator information joins a non-campus object with
   an operator at the same addr:housenumber and addr:street within NEIGHBOR_GAP_M, unless objects
   with that address name operators that disagree (a carrier hotel). A named one may be another
   tenant, so it does not join this way.
6. A point inside a building: a node inside a building's bounding box joins it when the two and
   every other node in that box have compatible operators.

When regions is given (ref -> county FIPS), no rule joins two objects in different regions, so a
cluster never crosses a county or state line. Distances are gaps between bounding boxes, not
between centers, so two large buildings that touch join although their centers are far apart.
Two campus objects join each other directly only by name (rule 4) or inside one site. The result
does not depend on input order.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

from atlas.text import normalize_name, normalize_org

ObjectKind = Literal["campus", "site", "building", "point"]
AREA_KINDS: tuple[ObjectKind, ...] = ("campus", "site")  # objects that stand for a whole site

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_DEGREE_LAT = math.pi * EARTH_RADIUS_M / 180  # about 111,195 m, the same sphere
CONTAIN_MARGIN_M = 30.0
DEFAULT_RADIUS_M = 300.0
NEIGHBOR_GAP_M = 50.0  # rule 3; 100 m chained unnamed buildings across 1.5 km on 2026-10-07
# Rule 4. The same specific name 380 m apart was one campus on 2026-10-08 (Meta Henrico, Meta
# Sarpy, Compugen New Albany); two TulsaConnect nodes 558 m apart were not.
NAME_GAP_M = 500.0
# Words that say nothing about which site a name means (rule 4, specific_name).
GENERIC_NAME_WORDS = frozenset(
    {
        "a",
        "and",
        "at",
        "bldg",
        "building",
        "buildings",
        "campus",
        "center",
        "centers",
        "centre",
        "centres",
        "co",
        "company",
        "corp",
        "data",
        "datacenter",
        "datacenters",
        "datacentre",
        "datacentres",
        "dc",
        "east",
        "facility",
        "hall",
        "inc",
        "llc",
        "north",
        "of",
        "phase",
        "site",
        "south",
        "the",
        "west",
    }
)
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

    def gap_m(self, other: Bounds) -> float:
        """The distance in metres between the two boxes, 0 when they touch or overlap
        (equirectangular at the mean latitude, like area_m2)."""
        lat = (self.minlat + self.maxlat + other.minlat + other.maxlat) / 4
        dlat = max(0.0, other.minlat - self.maxlat, self.minlat - other.maxlat)
        dlon = max(0.0, other.minlon - self.maxlon, self.minlon - other.maxlon)
        return math.hypot(
            dlat * METERS_PER_DEGREE_LAT,
            dlon * METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)),
        )


Polyline = tuple[tuple[float, float], ...]  # (lat, lon) vertices


def point_in_outline(lat: float, lon: float, outline: Sequence[Polyline]) -> bool:
    """Even-odd test against every segment of the outline's polylines. A way's outline is one
    closed ring; a multipolygon's is its member ways, whose segments close the rings together, so
    the rings need not be assembled and inner rings are holes."""
    inside = False
    for line in outline:
        for (lat1, lon1), (lat2, lon2) in pairwise(line):
            if (lat1 > lat) != (lat2 > lat):
                cross = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
                if lon < cross:
                    inside = not inside
    return inside


@dataclass(frozen=True)
class OsmObject:
    """One Overpass element. lat/lon is the node position or the bounding-box midpoint. outline is
    the polygon (Overpass `out geom`) when the query returned it, which it does for sites only."""

    ref: str  # "node/1" | "way/1" | "relation/1"
    lat: float
    lon: float
    bounds: Bounds | None
    tags: Mapping[str, str] = field(hash=False)
    kind: ObjectKind
    outline: tuple[Polyline, ...] | None = field(default=None, hash=False, repr=False)

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

    @property
    def has_operator(self) -> bool:
        """True when the object has a usable operator or an operator:wikidata."""
        return self.operator_key is not None or self.operator_qid is not None

    @property
    def extent(self) -> Bounds:
        """The bounding box, or the point itself for a node."""
        return self.bounds or Bounds(self.lat, self.lon, self.lat, self.lon)

    @property
    def address_key(self) -> tuple[str, str] | None:
        """(addr:housenumber, addr:street), normalized, when the object has both."""
        number = normalize_name(self.tags.get("addr:housenumber", ""))
        street = normalize_name(self.tags.get("addr:street", ""))
        return (number, street) if number and street else None

    def area_m2(self) -> float:
        return self.bounds.area_m2() if self.bounds is not None else 0.0

    def gap_m(self, other: OsmObject) -> float:
        """The distance in metres between the two objects' extents, 0 when they overlap."""
        return self.extent.gap_m(other.extent)

    def covers(self, lat: float, lon: float, *, margin_m: float = CONTAIN_MARGIN_M) -> bool:
        """True when the point lies inside the outline, or, without one, inside the bounding box
        expanded by margin_m. A node covers nothing."""
        if self.outline:
            inside_box = self.bounds is None or self.bounds.contains(lat, lon)
            return inside_box and point_in_outline(lat, lon, self.outline)
        return self.bounds is not None and self.bounds.expanded(margin_m).contains(lat, lon)


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
        """The campus and site members."""
        return tuple(m for m in self.members if m.kind in AREA_KINDS)


def _word_prefix(a: str, b: str) -> bool:
    """True when the shorter of two normalized names is the start of the longer, word by word."""
    wa, wb = a.split(), b.split()
    n = min(len(wa), len(wb))
    return n > 0 and wa[:n] == wb[:n]


def operators_compatible(obj: OsmObject, campus: OsmObject) -> bool:
    """False only when obj and campus carry comparable operator information that disagrees.

    When both have an operator:wikidata, the ids decide. Otherwise the normalized operators are
    compared, and one that starts the other agrees with it ("Amazon" and "Amazon Web Services
    us-east-2 datacenter" both agree with "Amazon Web Services"). An object that lacks what the
    other has is compatible.
    """
    qa, qb = obj.operator_qid, campus.operator_qid
    if qa is not None and qb is not None:
        return qa == qb
    ka, kb = obj.operator_key, campus.operator_key
    if ka is not None and kb is not None:
        return _word_prefix(ka, kb)
    return True


def _all_compatible(objects: Sequence[OsmObject]) -> bool:
    return all(operators_compatible(a, b) for i, a in enumerate(objects) for b in objects[i + 1 :])


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
    """The smallest compatible campus (or site) that covers obj's center (rule 1)."""
    hits = [
        c
        for c in campuses
        if c.ref != obj.ref
        and c.covers(obj.lat, obj.lon, margin_m=margin_m)
        and operators_compatible(obj, c)
    ]
    if not hits:
        return None
    return min(hits, key=lambda c: (c.area_m2(), ref_key(c.ref)))


def operator_conflicts(objects: Sequence[OsmObject]) -> list[tuple[OsmObject, OsmObject]]:
    """(inner, outer) pairs that rule 1 or 6 would join but for operators that disagree: an object
    whose center a campus or site covers, or a node inside a building's bounding box. Sorted by
    the refs."""
    areas = [o for o in objects if o.kind in AREA_KINDS]
    buildings = [o for o in objects if o.kind == "building" and o.bounds is not None]
    found: list[tuple[OsmObject, OsmObject]] = []
    for obj in objects:
        if obj.kind == "site":
            continue  # rule 1 joins nothing into a site but campuses, buildings and points
        outers = [
            a
            for a in areas
            if a.ref != obj.ref
            and (obj.kind != "campus" or a.kind == "site")
            and a.covers(obj.lat, obj.lon)
        ]
        if obj.kind == "point":
            outers += [b for b in buildings if b.extent.contains(obj.lat, obj.lon)]
        found += [(obj, o) for o in outers if not operators_compatible(obj, o)]
    return sorted(found, key=lambda pair: (ref_key(pair[0].ref), ref_key(pair[1].ref)))


def operator_words(objects: Iterable[OsmObject]) -> frozenset[str]:
    """Every word of every operator and operator:short in objects (normalized), for
    specific_name."""
    words: set[str] = set()
    for o in objects:
        for value in (o.operator, o.tags.get("operator:short")):
            if value:
                words.update(normalize_name(value).split())
                words.update(normalize_org(value).split())
    return frozenset(words)


def specific_name(name: str | None, operators: frozenset[str]) -> str | None:
    """normalize_name(name) when it says which site it means: some word is not a word of an
    operator, not in GENERIC_NAME_WORDS, not a number and longer than one letter. "Meta Sarpy
    Data Center" is specific; "Google", "Amazon Web Services" and "Building 2" are not."""
    key = normalize_name(name) if name else ""
    words = [
        w
        for w in key.split()
        if w not in operators and w not in GENERIC_NAME_WORDS and not w.isdigit() and len(w) > 1
    ]
    return key if words else None


def choose_representative(members: Sequence[OsmObject]) -> OsmObject:
    """The campus or site object (the largest if several), else the largest way or relation by
    bounding box, else the node with the lowest id."""
    campuses = [m for m in members if m.kind in AREA_KINDS]
    if campuses:
        return min(campuses, key=lambda m: (-m.area_m2(), ref_key(m.ref)))
    shapes = [m for m in members if m.osm_type in ("way", "relation")]
    if shapes:
        return min(shapes, key=lambda m: (-m.area_m2(), ref_key(m.ref)))
    return min(members, key=lambda m: ref_key(m.ref))


Join = Callable[[OsmObject, OsmObject], None]


def _join_close(
    join: Join,
    groups: Iterable[Sequence[OsmObject]],
    limit_m: float,
    accept: Callable[[OsmObject, OsmObject], bool] | None = None,
) -> None:
    """Join every two objects of a group whose extents are within limit_m of each other (and that
    accept allows).

    Each group is swept in order of the extents' southern edge, so only pairs that overlap in
    latitude (within limit_m) are measured.
    """
    dlat = limit_m / METERS_PER_DEGREE_LAT
    for members in groups:
        swept = sorted(members, key=lambda o: (o.extent.minlat, ref_key(o.ref)))
        for i, a in enumerate(swept):
            north = a.extent.maxlat + dlat
            for b in swept[i + 1 :]:
                if b.extent.minlat > north:
                    break
                if a.gap_m(b) <= limit_m and (accept is None or accept(a, b)):
                    join(a, b)


def dissolve(
    objects: Sequence[OsmObject],
    *,
    radius_m: float = DEFAULT_RADIUS_M,
    regions: Mapping[str, str | None] | None = None,
) -> list[Cluster]:
    """Group objects into clusters (see the module docstring). regions (ref -> county FIPS) keeps
    every cluster inside one region. Clusters are sorted by their representative's ref_key, and
    each cluster's members by ref_key."""
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

    def join(a: OsmObject, b: OsmObject) -> None:
        # Every set is one region, so comparing the two objects keeps it so.
        if regions is None or regions.get(a.ref) == regions.get(b.ref):
            uf.union(index[a.ref], index[b.ref])

    campuses = [o for o in ordered if o.kind == "campus"]
    sites = [o for o in ordered if o.kind == "site"]
    others = [o for o in ordered if o.kind not in AREA_KINDS]

    # Rule 1: containment in a campus bounding box, and of anything but a site in a site.
    for obj in others:
        campus = containing_campus(obj, campuses)
        if campus is not None:
            join(obj, campus)
    for obj in others + campuses:
        site = containing_campus(obj, sites)
        if site is not None:
            join(obj, site)

    # Rule 2: same operator (name or Wikidata id), extents within radius_m.
    by_operator: dict[str, list[OsmObject]] = {}
    for obj in others:
        if obj.operator_key is not None:
            by_operator.setdefault("name:" + obj.operator_key, []).append(obj)
        if obj.operator_qid is not None:
            by_operator.setdefault("qid:" + obj.operator_qid, []).append(obj)
    _join_close(join, by_operator.values(), radius_m)

    # Rule 3: objects without operator information, by name (or both unnamed) within
    # NEIGHBOR_GAP_M, or by street address within radius_m.
    by_name: dict[str, list[OsmObject]] = {}
    by_address: dict[tuple[str, str], list[OsmObject]] = {}
    for obj in others:
        if obj.has_operator:
            continue
        by_name.setdefault(normalize_name(obj.name) if obj.name else "", []).append(obj)
        if obj.address_key is not None:
            by_address.setdefault(obj.address_key, []).append(obj)
    _join_close(join, by_name.values(), NEIGHBOR_GAP_M)
    _join_close(join, by_address.values(), radius_m)

    # Rule 4: the same specific name, compatible operators, extents within NAME_GAP_M. The
    # operators of both clusters must agree, so an object without an operator never bridges two
    # operators' clusters.
    words = operator_words(ordered)
    by_specific: dict[str, list[OsmObject]] = {}
    for obj in ordered:
        key = specific_name(obj.name, words)
        if key is not None:
            by_specific.setdefault(key, []).append(obj)
    with_operator: dict[int, list[OsmObject]] = {}
    for obj in ordered:
        if obj.has_operator:
            with_operator.setdefault(uf.find(index[obj.ref]), []).append(obj)

    def agree(a: OsmObject, b: OsmObject) -> bool:
        ra, rb = uf.find(index[a.ref]), uf.find(index[b.ref])
        return ra == rb or all(
            operators_compatible(x, y)
            for x in with_operator.get(ra, [])
            for y in with_operator.get(rb, [])
        )

    def join_named(a: OsmObject, b: OsmObject) -> None:
        ra, rb = uf.find(index[a.ref]), uf.find(index[b.ref])
        join(a, b)
        root = uf.find(index[a.ref])
        if ra != rb and root == uf.find(index[b.ref]):
            with_operator[root] = with_operator.pop(ra, []) + with_operator.pop(rb, [])

    _join_close(join_named, by_specific.values(), NAME_GAP_M, agree)

    # Rule 5: an unnamed object without an operator and one with an operator at the same address.
    at_address: dict[tuple[str, str], list[OsmObject]] = {}
    for obj in others:
        if obj.address_key is not None:
            at_address.setdefault(obj.address_key, []).append(obj)
    for same in at_address.values():
        named = [o for o in same if o.has_operator]
        if not named or not _all_compatible(named):
            continue
        for obj in same:
            if not obj.has_operator and not obj.name:
                for other in named:
                    if obj.gap_m(other) <= NEIGHBOR_GAP_M:
                        join(obj, other)

    # Rule 6: a node inside a building's bounding box.
    points = [o for o in others if o.kind == "point"]
    for building in others:
        if building.kind != "building" or building.bounds is None:
            continue
        inside = [p for p in points if building.bounds.contains(p.lat, p.lon)]
        if inside and _all_compatible([building, *inside]):
            for p in inside:
                join(p, building)

    grouped: dict[int, list[OsmObject]] = {}
    for obj in ordered:
        grouped.setdefault(uf.find(index[obj.ref]), []).append(obj)
    clusters = [
        Cluster(members=tuple(members), representative=choose_representative(members))
        for members in grouped.values()
    ]
    return sorted(clusters, key=lambda c: ref_key(c.representative.ref))
