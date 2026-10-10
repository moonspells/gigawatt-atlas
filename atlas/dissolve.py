"""Dissolve OpenStreetMap data center objects into campuses (07 §4.2 step 2).

The Overpass query returns campuses (landuse-style polygons tagged as a data center without a
building tag), sites (polygons tagged industrial=data_centre and nothing else that marks a data
center, and the construction or data center site polygons that hold data center objects),
buildings and points. One campus record should stand for each site, so objects are joined with
union-find:

1. Containment: an object whose center lies inside a campus object (its outline when the query
   returned one, else its bounding box expanded by 30 m) joins that campus; inside several, it
   joins the smallest one. Any object (a campus too) whose center lies inside a site polygon (its
   outline, else its box + 30 m) joins the smallest such site. A campus or site and an object
   whose operators disagree do not join this way (operators_compatible), because a bounding box
   is only an approximation of the campus polygon and in dense areas such as Ashburn it covers
   other operators' buildings. An area without an operator whose contents name operators that
   disagree holds only the objects that share its street address (a construction site's box
   over two companies' buildings).
2. Operator distance: two non-campus objects whose bounding boxes (a node is its point) are within
   radius_m of each other join when they have the same normalize_org(operator) or the same
   operator:wikidata. An object without operator information whose name starts with an operator
   named in the input counts as that operator's (implied_operators: "Google Leesburg Building 3",
   "Compass Data Center"). Two campus or site objects (not containers) of one operator that
   adjoin join the same way (adjoining_gap_m: outlines within ADJOIN_M, or overlapping boxes
   when an outline is missing): a campus and its expansion site (Meta New Albany and Meta LCO
   3, which touch) or two outers of one site (Google Lenoir, 108 m), unless each lists a street
   address and they differ: two projects of one operator (Microsoft's Project Alluvion and
   Project Ginger East, 126 m apart in West Des Moines) are a same_operator_nearby note.
3. Neighbours without an operator: two non-campus objects that both have no operator information
   join when their bounding boxes are within NEIGHBOR_GAP_M and they have the same
   normalize_name(name) or neither has a name, or when they have the same addr:housenumber and
   addr:street and their boxes are within radius_m. Two unnamed halls of at least LARGE_HALL_M2
   each join within LARGE_HALL_GAP_M.
4. Same name: two objects of any kind with the same specific normalize_name(name) (one that says
   more than an operator and words such as "data center" or "building", see specific_name) join
   when their boxes are within NAME_GAP_M and the operators of their two clusters are compatible,
   so an operator-less node joins the polygons of the same name but never bridges two operators.
   Sibling names, which differ only in a number or a final direction ("KOMO Plaza East" and
   "West", "EAT12" and "EAT13", "PowerHouse Pacific Building 1" and "2"), join the same way
   within radius_m.
5. Same address: an unnamed object without operator information joins a non-campus object with
   an operator at the same addr:housenumber and addr:street within NEIGHBOR_GAP_M, unless objects
   with that address name operators that disagree (a carrier hotel). A named one may be another
   tenant, so it does not join this way.
6. A point inside a building: a node inside a building's bounding box joins it when the two and
   every other node in that box have compatible operators.
7. An unnamed neighbour in one operator's row: a building without a name or operator
   information, in a cluster without an operator, joins the nearest building with an operator
   within NEIGHBOR_GAP_M when every object with operator information within radius_m names that
   one operator (904 Quality Way, 18 m from Digital Realty DFW18 among the Digital Dallas
   buildings). A node marks where a tenant is, not a row, so it neither joins nor is joined.
8. A numbered sibling or a street neighbour just beyond the radius: two non-campus objects of one
   operator (rule 2's keys) whose boxes are more than radius_m but at most NAME_GAP_M apart join
   when one's name continues a numbered series of the other's cluster (series_key: "Iron Mountain
   VA-6", 364 m from VA-5, with VA-1 to VA-5 and VA-7) or they have the same addr:street (Digital
   Realty IAD41, 348 m from IAD39 on Round Table Plaza), unless something separates them
   (separation): addr:postcodes that differ, an object of another operator between them, a site
   polygon of each, or two points (a point shows where a tenant is, not a building). A separated
   pair is a same_operator_nearby note.

After the joins, same_operator_nearby notes name the clusters that may still be one site: one
operator's numbered series or street split within SERIES_GAP_M (CloudHQ LC1 to LC3 and LC4 to
LC14, 650 m apart), or members that name one web page within SITE_PAGE_GAP_M; and
tenant_building notes name a lone building of another operator within TENANT_GAP_M of one
operator's row of buildings, with no third operator around it (Rackspace's DFW3, which Digital
Realty lists as DFW25 of its Digital Dallas campus).

An element tagged with another primary use (amenity, shop, tourism, healthcare: the post office
in the Terminal Annex) or whose operator tag names a general contractor (HITT on a construction
site) has no operator for these rules: its operator tag is not the data center's.

When regions is given (ref -> county FIPS), no rule joins two objects in different regions,
except containment in an outline (a campus inside one site polygon is one facility across a
county line); such a join and every join the regions block are DissolveNotes. Distances are gaps
between bounding boxes, not between centers, so two large buildings that touch join although
their centers are far apart. Two campus objects join each other directly only by operator
distance (rule 2), by name (rule 4) or inside one site. The result does not depend on input
order.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
# Rule 3, large halls: the unnamed halls of one development stand 74-100 m apart (Stream San
# Antonio III, QTS Atlanta DC3 and DC4, a Prince William construction site, 2026-10-09), and a
# hall is far larger than the sheds and offices that chained at 100 m.
LARGE_HALL_M2 = 5_000.0
LARGE_HALL_GAP_M = 100.0
# Keys whose value says an element's primary use is something other than housing computers: its
# operator tag names that use (the United States Postal Service on the Terminal Annex, which
# holds CoreSite LA2), not the data center's operator (07 §2.2).
OTHER_USE_KEYS = ("amenity", "shop", "tourism", "healthcare", "healthcare:speciality")
# General contractors that mappers enter as the operator of a construction site: the builder, not
# the data center's operator (HITT on Digital Realty's Manassas site, 2026-10-09).
GENERAL_CONTRACTORS = frozenset(
    normalize_org(name)
    for name in (
        "HITT",
        "HITT Contracting",
        "Clark Construction",
        "Clark Construction Group",
        "DPR Construction",
        "Holder Construction",
        "Turner Construction",
        "Whiting-Turner",
        "The Whiting-Turner Contracting Company",
        "Mortenson",
        "M. A. Mortenson",
        "JE Dunn",
        "JE Dunn Construction",
        "Gray Construction",
        "Gilbane",
        "Gilbane Building Company",
        "Skanska",
        "Skanska USA",
        "Hensel Phelps",
        "McCarthy Building Companies",
        "Fortis Construction",
        "Structure Tone",
        "Kiewit",
    )
)
MIN_TYPO_LENGTH = 6  # operators this long that differ by one letter are one ("CyrysOne")
NOTE_KEYS = ("note", "fixme", "FIXME", "description")
# landuse=* of the container polygons (OsmObject.container).
CONTAINER_LANDUSE = frozenset({"construction", "industrial", "commercial"})
_APPROXIMATE_RE = re.compile(r"approximat", re.IGNORECASE)
# Rule 4. The same specific name 380 m apart was one campus on 2026-10-08 (Meta Henrico, Meta
# Sarpy, Compugen New Albany); two TulsaConnect nodes 558 m apart were not. Also rule 8's limit.
NAME_GAP_M = 500.0
# same_operator_nearby notes (after the joins): RagingWire CA3 is 720 m from CA2 and CloudHQ LC4
# 650 m from LC3, each one campus by the operator's own page (2026-10-09); AWS's Morrow County
# groups, 512 m and more apart, are separate sites (and have same-name items).
SERIES_GAP_M = 1_000.0
# Rule 2 between areas: two outlines this close adjoin (Google Lenoir's two outers, 108 m apart;
# Meta New Albany and its LCO 3 expansion site touch). Without both outlines, the boxes must
# overlap: a box overstates its polygon.
ADJOIN_M = 150.0
DUPLICATE_OVERLAP = 0.5  # share of the smaller box: an unnamed way drawn over another building
SITE_PAGE_GAP_M = 2_000.0  # members that give one web page (CloudHQ's LC campus page)
# tenant_building notes: Rackspace DFW3 is 42 m (box gap) from Digital Realty's Digital Dallas
# row of six buildings; Digital Realty lists it as DFW25 of that campus.
TENANT_GAP_M = 150.0
TENANT_ROW = 3  # buildings of one operator that make a row
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


def ring_area_m2(outline: Sequence[Polyline] | None) -> float | None:
    """The area in square metres of an outline that is one closed ring (a way's `out geom`), by
    the shoelace formula on an equirectangular projection at the ring's mean latitude; None for
    a relation's member ways, which this does not assemble into rings."""
    if not outline or len(outline) != 1:
        return None
    ring = outline[0]
    if len(ring) < 4 or ring[0] != ring[-1]:
        return None
    lat0 = sum(lat for lat, _ in ring) / len(ring)
    kx = METERS_PER_DEGREE_LAT * math.cos(math.radians(lat0))
    twice = sum(
        (lon1 * kx) * (lat2 * METERS_PER_DEGREE_LAT) - (lon2 * kx) * (lat1 * METERS_PER_DEGREE_LAT)
        for (lat1, lon1), (lat2, lon2) in pairwise(ring)
    )
    return abs(twice) / 2


def _segment_gap_m(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float], kx: float
) -> float:
    """Metres from point p to segment ab, (lat, lon) on an equirectangular projection (kx: metres
    per degree of longitude)."""
    ky = METERS_PER_DEGREE_LAT
    px, py, ax, ay, bx, by = p[1] * kx, p[0] * ky, a[1] * kx, a[0] * ky, b[1] * kx, b[0] * ky
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def outline_gap_m(a: Sequence[Polyline], b: Sequence[Polyline]) -> float:
    """The distance in metres between two outlines: 0 when a vertex of one lies inside the other,
    else the least distance from a vertex of one to a segment of the other."""
    points = [p for line in (*a, *b) for p in line]
    lat0 = sum(p[0] for p in points) / len(points) if points else 0.0
    kx = METERS_PER_DEGREE_LAT * math.cos(math.radians(lat0))
    best = math.inf
    for one, other in ((a, b), (b, a)):
        for line in one:
            for p in line:
                if point_in_outline(p[0], p[1], other):
                    return 0.0
                for seg in other:
                    for s, e in pairwise(seg):
                        best = min(best, _segment_gap_m(p, s, e, kx))
    return best


def box_overlap(a: Bounds, b: Bounds) -> float:
    """The share of the smaller box that the two boxes have in common."""
    dlat = min(a.maxlat, b.maxlat) - max(a.minlat, b.minlat)
    dlon = min(a.maxlon, b.maxlon) - max(a.minlon, b.minlon)
    if dlat <= 0 or dlon <= 0:
        return 0.0
    smaller = min(
        (a.maxlat - a.minlat) * (a.maxlon - a.minlon), (b.maxlat - b.minlat) * (b.maxlon - b.minlon)
    )
    return dlat * dlon / smaller if smaller > 0 else 0.0


@dataclass(frozen=True)
class OsmObject:
    """One Overpass element. lat/lon is the node position or the bounding-box midpoint. outline is
    the polygon (Overpass `out geom`) when the query returned it, which it does for areas only.
    implied_operator is set by dissolve() on its working copies only (implied_operators)."""

    ref: str  # "node/1" | "way/1" | "relation/1"
    lat: float
    lon: float
    bounds: Bounds | None
    tags: Mapping[str, str] = field(hash=False)
    kind: ObjectKind
    outline: tuple[Polyline, ...] | None = field(default=None, hash=False, repr=False)
    implied_operator: str | None = field(default=None, hash=False, compare=False, repr=False)

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
    def other_use(self) -> str | None:
        """ "amenity=post_office" when the element is tagged as a feature of another primary use
        (OTHER_USE_KEYS), else None."""
        for key in OTHER_USE_KEYS:
            value = self.tags.get(key)
            if value and value != "no":
                return f"{key}={value}"
        return None

    @property
    def builder(self) -> str | None:
        """The operator tag when it names a general contractor (GENERAL_CONTRACTORS), else None."""
        op = self.tags.get("operator")
        return op if op and normalize_org(op) in GENERAL_CONTRACTORS else None

    @property
    def operator(self) -> str | None:
        """The operator tag, unless it names another primary use's operator (other_use) or the
        site's builder (builder): then the element has no data center operator."""
        op = self.tags.get("operator") or None
        if op is None or self.other_use is not None or self.builder is not None:
            return None
        return op

    @property
    def operator_key(self) -> str | None:
        """normalize_org(operator), else the implied operator, or None when there is neither."""
        op = self.operator
        key = normalize_org(op) if op else ""
        return key or self.implied_operator or None

    @property
    def operator_qid(self) -> str | None:
        if self.other_use is not None or self.builder is not None:
            return None
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

    @property
    def addresses(self) -> frozenset[tuple[str, str]]:
        """Every (house number, street) the object's addr:housenumber and addr:street name,
        normalized: OSM separates several values with ";" ("22271;22275;22285;22295" Lockridge
        Road, a site that lists its buildings' numbers)."""
        numbers = [normalize_name(n) for n in self.tags.get("addr:housenumber", "").split(";")]
        streets = [normalize_name(s) for s in self.tags.get("addr:street", "").split(";")]
        return frozenset((n, s) for n in numbers for s in streets if n and s)

    @property
    def container(self) -> bool:
        """True for a site polygon that is not itself tagged as a data center: a construction
        site or a named data center site that OSM tags only with landuse (the query's third
        statement), which joins what lies in it but never leads a record when a data center area
        is there too."""
        return (
            self.kind == "site"
            and self.tags.get("industrial") not in ("data_centre", "data_center")
            and self.tags.get("landuse") in CONTAINER_LANDUSE
        )

    @property
    def planned(self) -> bool:
        """True for an area that OSM tags as planned or being built: landuse=construction, a
        construction or proposed tag, or a proposed: or construction: lifecycle key ("Meta LCO 3
        Project", landuse=construction; Apple's proposed:landuse=industrial site at Waukee)."""
        return (
            self.tags.get("landuse") == "construction"
            or "construction" in self.tags
            or "proposed" in self.tags
            or any(k.startswith(("proposed:", "construction:")) for k in self.tags)
        )

    @property
    def approximate_note(self) -> str | None:
        """The note, fixme or description that says the element's position is approximate."""
        for key in NOTE_KEYS:
            text = self.tags.get(key, "")
            if _APPROXIMATE_RE.search(text):
                return text
        return None

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


def one_edit(a: str, b: str) -> bool:
    """True when two different strings of at least MIN_TYPO_LENGTH characters differ by one
    inserted, deleted or replaced character ("cyrysone" and "cyrusone")."""
    if a == b or min(len(a), len(b)) < MIN_TYPO_LENGTH or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        return a[i + 1 :] == b[i + 1 :]
    return a[i:] == b[i + 1 :]


def operators_compatible(obj: OsmObject, campus: OsmObject) -> bool:
    """False only when obj and campus carry comparable operator information that disagrees.

    When both have an operator:wikidata, the ids decide. Otherwise the normalized operators are
    compared, and one that starts the other agrees with it ("Amazon" and "Amazon Web Services
    us-east-2 datacenter" both agree with "Amazon Web Services"), also once generic words are
    set aside (operator_group: "Bank of America" and "Bank America"), as do two that differ by
    one letter (one_edit: "CyrysOne", a typo, and "CyrusOne"). An object that lacks what the
    other has is compatible.
    """
    qa, qb = obj.operator_qid, campus.operator_qid
    if qa is not None and qb is not None:
        return qa == qb
    ka, kb = obj.operator_key, campus.operator_key
    if ka is not None and kb is not None:
        return (
            _word_prefix(ka, kb)
            or _word_prefix(operator_group(ka), operator_group(kb))
            or one_edit(ka, kb)
        )
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


def _distinct_words(value: str) -> list[str]:
    """The words of normalize_org(value) that are not GENERIC_NAME_WORDS ("Compass Datacenters"
    and "Compass Data Center" both give ["compass"])."""
    return [w for w in normalize_org(value).split() if w not in GENERIC_NAME_WORDS]


def operator_group(key: str) -> str:
    """An operator key without its generic words, for rule 2: "powerhouse data centers" and
    "powerhouse" are one operator, "compass datacenters" is "compass"; a key that is only generic
    words stays as it is."""
    return " ".join(w for w in key.split() if w not in GENERIC_NAME_WORDS) or key


OperatorForms = Mapping[tuple[str, ...], frozenset[str]]


def operator_forms(objects: Iterable[OsmObject]) -> dict[tuple[str, ...], frozenset[str]]:
    """The distinctive words of every operator and operator:short in the input (at least one word
    of three letters or more that is not a number) -> the operator_group of the operators they
    stand for."""
    forms: dict[tuple[str, ...], set[str]] = {}
    for o in objects:
        op = o.operator
        key = normalize_org(op) if op else ""
        if not key:
            continue
        for value in (op, o.tags.get("operator:short")):
            words = tuple(_distinct_words(value)) if value else ()
            if words and any(len(w) >= 3 and not w.isdigit() for w in words):
                forms.setdefault(words, set()).add(operator_group(key))
    return {f: frozenset(g) for f, g in forms.items()}


def _leading_operator(words: Sequence[str], forms: OperatorForms) -> tuple[int, frozenset[str]]:
    """(the number of leading words that name an operator, the operator groups they name)."""
    best = [f for f in forms if tuple(words[: len(f)]) == f]
    if not best:
        return 0, frozenset()
    longest = max(len(f) for f in best)
    return longest, frozenset().union(*(forms[f] for f in best if len(f) == longest))


def implied_operators(
    objects: Iterable[OsmObject], forms: OperatorForms | None = None
) -> dict[str, str]:
    """ref -> operator group for the objects without operator information whose name starts with
    the distinctive words of an operator (or operator:short) named in the input: "Google
    Leesburg Building 3" for Google, "CyrusOne San Antonio IV" for CyrusOne, "Compass Data
    Center" for Compass Datacenters, "PowerHouse Pacific Building 2" for PowerHouse. The longest
    such operator wins; a name that two operators fit equally, or an operator whose words are
    all short or generic, implies none."""
    objects = list(objects)
    forms = operator_forms(objects) if forms is None else forms
    found: dict[str, str] = {}
    for o in objects:
        if o.has_operator or not o.name:
            continue
        _, groups = _leading_operator(_distinct_words(o.name), forms)
        if len(groups) == 1:
            found[o.ref] = next(iter(groups))
    return found


_DIRECTIONS = frozenset({"east", "west", "north", "south"})
_ROMAN_RE = re.compile(r"^(?:ii|iii|iv|vi|vii|viii|ix|xi|xii)$")
_ATTACHED_NUMBER_RE = re.compile(r"^([a-z]{2,})(\d+[a-z]?)$")


def sibling_stem(name: str | None, forms: OperatorForms) -> str | None:
    """The name without its building numbers and a final direction or Roman numeral, when it had
    one: "KOMO Plaza East" and "KOMO Plaza West" give "komo plaza", "EAT12" and "EAT13" give
    "eat", "PowerHouse Pacific Building 2" gives "powerhouse pacific building". None when nothing
    was dropped, for a name that is a street address, or when the stem says nothing once the
    operator it starts with is set aside ("Building 2", "Equinix DC10": only generic words)."""
    words = normalize_name(name).split() if name else []
    if not words or words[0][:1].isdigit():
        return None  # a name that is a street address ("3433 South 120th Place")
    stem: list[str] = []
    dropped = False
    for w in words:
        attached = _ATTACHED_NUMBER_RE.match(w)
        if w.isdigit() or re.fullmatch(r"\d+[a-z]", w):
            dropped = True
        elif attached is not None:
            stem.append(attached.group(1))
            dropped = True
        else:
            stem.append(w)
    if len(stem) > 1 and (stem[-1] in _DIRECTIONS or _ROMAN_RE.match(stem[-1])):
        stem.pop()
        dropped = True
    if not dropped:
        return None
    lead, _ = _leading_operator([w for w in stem if w not in GENERIC_NAME_WORDS], forms)
    rest = [w for w in stem if w not in GENERIC_NAME_WORDS][lead:]
    return " ".join(stem) if any(len(w) > 1 and not w.isdigit() for w in rest) else None


_SERIES_WORD_RE = re.compile(r"^([a-z]*)(\d+)$")


def series_key(name: str | None, forms: OperatorForms) -> tuple[str, int] | None:
    """(the name without its last number, that number) for a name that ends in a building
    number of a series: "Iron Mountain VA-6" gives ("iron mountain va", 6), "Digital Realty IAD41"
    ("digital realty iad", 41), "QTS NAL1 DC2" ("qts nal1 dc", 2), so that "QTS NAL 2 DC1" is
    another series. None when the last word carries no number, or when sibling_stem finds no
    stem (a street address, or nothing beyond the operator and generic words: "Building 2")."""
    if sibling_stem(name, forms) is None:
        return None
    words = normalize_name(name).split() if name else []
    found = _SERIES_WORD_RE.match(words[-1]) if words else None
    if found is None:
        return None
    head = [*words[:-1], found.group(1)] if found.group(1) else words[:-1]
    return " ".join(head), int(found.group(2))


def _street_key(obj: OsmObject) -> str | None:
    """normalize_name(addr:street) when the object names one street (not a ";" list)."""
    street = obj.tags.get("addr:street", "")
    if ";" in street:
        return None
    return normalize_name(street) or None


def _postcode(obj: OsmObject) -> str | None:
    """The five-digit ZIP code of addr:postcode ("20191-3407" is "20191"), else None."""
    code = obj.tags.get("addr:postcode", "").strip()[:5]
    return code if len(code) == 5 and code.isdigit() else None


def _web_page(obj: OsmObject) -> str | None:
    """The website tag when it names a page, not a bare domain ("https://cloudhq.com/campus/
    lc-campus/", not "https://nocroom.com"), without its scheme, "www." and a final "/". It only
    compares clusters; no record publishes it."""
    url = obj.tags.get("website", "").strip().casefold()
    url = re.sub(r"^https?://(?:www\.)?", "", url).rstrip("/")
    return url if "/" in url else None


def _union_box(a: Bounds, b: Bounds) -> Bounds:
    return Bounds(
        min(a.minlat, b.minlat),
        min(a.minlon, b.minlon),
        max(a.maxlat, b.maxlat),
        max(a.maxlon, b.maxlon),
    )


def mixed_anchors(area: OsmObject, inside: Sequence[OsmObject]) -> list[OsmObject]:
    """For an area without an operator whose contents name operators that disagree: the objects
    it surely holds. Those at its own street address (a construction site's polygon at 10051
    Brickyard Way and the Digital Realty building there, not the Amazon buildings its box also
    covers), else those of the operator most of its contents name, when one does (Digital
    Realty's halls in the Digital Loudoun Plaza polygons, not two AWS buildings in their box),
    else none. The area then holds the anchors and the contents compatible with all of them."""
    at_address = [o for o in inside if shares_address(o, area)]
    if at_address:
        return at_address
    counts: dict[str, list[OsmObject]] = {}
    for o in inside:
        key = o.operator_key or o.operator_qid
        if key is not None:
            counts.setdefault(key, []).append(o)
    if not counts:
        return []
    ranked = sorted(counts.values(), key=len, reverse=True)
    if len(ranked) > 1 and len(ranked[0]) == len(ranked[1]):
        return []
    return ranked[0]


def shares_address(inner: OsmObject, area: OsmObject) -> bool:
    """True when one of inner's (house number, street) pairs is one of area's (a list separated
    by ";" counts each number)."""
    return bool(inner.addresses & area.addresses)


NoteKind = Literal[
    "county_line",
    "county_blocked",
    "mixed_operators",
    "address_conflict",
    "same_operator_nearby",
    "tenant_building",
]


@dataclass(frozen=True)
class DissolveNote:
    """What dissolve() did or refused that a reviewer should see. refs is (object, other):

    - county_line: object joined the area `other` (its outline holds it) across a county line;
    - county_blocked: a rule would have joined the two but for a county line;
    - mixed_operators: the area `other` has no operator and holds objects whose operators
      disagree, so it held only those at its address, and object is one it did not hold;
    - address_conflict: object lies in the area `other` and shares its street address, but their
      operators disagree;
    - same_operator_nearby: the two, of one operator, may be one site that no rule joined (rule
      2 between areas with different addresses, a separated rule 8 pair, or a numbered series,
      street or web page split further apart); why says what links and what separates them;
    - tenant_building: object, a lone building of another operator, lies within TENANT_GAP_M of
      `other`'s row of one operator's buildings (a building of that campus tagged with its tenant,
      or a neighbour).
    """

    kind: NoteKind
    refs: tuple[str, str]
    rule: str = ""
    why: str = ""


@dataclass(frozen=True)
class DissolveResult:
    clusters: list[Cluster]
    notes: list[DissolveNote]


def choose_representative(members: Sequence[OsmObject]) -> OsmObject:
    """The campus or site object (the largest if several, one that is not planned first: an
    expansion site under construction or proposed does not lead the campus it joined by rule 2,
    Meta LCO 3 beside Meta New Albany; a container only when the cluster has no other), else the
    largest way or relation by bounding box, else the node with the lowest id."""
    campuses = [m for m in members if m.kind in AREA_KINDS and not m.container]
    campuses = campuses or [m for m in members if m.kind in AREA_KINDS]
    if campuses:
        return min(campuses, key=lambda m: (m.planned, -m.area_m2(), ref_key(m.ref)))
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


def adjoining_gap_m(a: OsmObject, b: OsmObject) -> float | None:
    """The gap between two areas when they adjoin (rule 2 between areas), else None: their
    outlines within ADJOIN_M, or, when either has no outline, boxes that overlap."""
    if a.outline and b.outline:
        if a.gap_m(b) > ADJOIN_M:
            return None
        gap = outline_gap_m(a.outline, b.outline)
        return gap if gap <= ADJOIN_M else None
    return 0.0 if a.gap_m(b) == 0 else None


def _who(obj: OsmObject) -> str:
    """The operator an object names, the operator its name implies, or its operator:wikidata."""
    return obj.operator or obj.implied_operator or obj.operator_qid or "none"


def _operator_keys(obj: OsmObject) -> list[str]:
    """Rule 2's keys: "name:" + operator_group(operator_key), "qid:" + operator:wikidata."""
    keys = []
    if obj.operator_key is not None:
        keys.append("name:" + operator_group(obj.operator_key))
    if obj.operator_qid is not None:
        keys.append("qid:" + obj.operator_qid)
    return keys


def _within(
    obj: OsmObject, pool: Sequence[OsmObject], south_edges: Sequence[float], limit_m: float
) -> list[OsmObject]:
    """The objects of pool (sorted by extent.minlat, whose values south_edges lists) other than
    obj whose extents are within limit_m of obj's."""
    dlat = limit_m / METERS_PER_DEGREE_LAT
    hi = bisect_right(south_edges, obj.extent.maxlat + dlat)
    return [
        o
        for o in pool[:hi]
        if o.ref != obj.ref
        and o.extent.maxlat >= obj.extent.minlat - dlat
        and obj.gap_m(o) <= limit_m
    ]


SeriesKeys = Mapping[str, tuple[str, int] | None]


def _series_link(
    a: OsmObject,
    b: OsmObject,
    group_a: Sequence[OsmObject],
    group_b: Sequence[OsmObject],
    keys: SeriesKeys,
) -> str | None:
    """What makes a and b (of two clusters) look like one site, or None: a name that continues
    the numbered series of the other cluster (one number apart from a member's; keys is
    series_key by ref), or the same addr:street."""
    for x, others in ((a, group_b), (b, group_a)):
        key = keys.get(x.ref)
        if key is None:
            continue
        for m in others:
            other = keys.get(m.ref)
            if other is not None and other[0] == key[0] and abs(other[1] - key[1]) == 1:
                low, high = (m, x) if other[1] < key[1] else (x, m)
                return f"{high.name!r} continues the series of {low.name!r}"
    street = _street_key(a)
    if street is not None and street == _street_key(b):
        return f"both are on {a.tags['addr:street']!r}"
    return None


def _separation(
    a: OsmObject,
    b: OsmObject,
    group_a: Sequence[OsmObject],
    group_b: Sequence[OsmObject],
    objects: Sequence[OsmObject],
) -> str | None:
    """What keeps a and b apart although rule 8 links them, or None: different ZIP codes, both
    points, a site polygon of each, or an object of another operator whose center lies in the box
    that spans the two."""
    pa, pb = _postcode(a), _postcode(b)
    if pa is not None and pb is not None and pa != pb:
        return f"their postcodes differ ({pa} and {pb})"
    if a.kind == "point" and b.kind == "point":
        return "both are points, which show where an operator is, not a building"
    areas_a = [m.ref for m in group_a if m.kind in AREA_KINDS]
    areas_b = [m.ref for m in group_b if m.kind in AREA_KINDS]
    if areas_a and areas_b:
        return f"each lies in its own site polygon ({areas_a[0]} and {areas_b[0]})"
    span = _union_box(a.extent, b.extent)
    for o in objects:
        if (
            o.ref not in (a.ref, b.ref)
            and o.has_operator
            and span.contains(o.lat, o.lon)
            and not (operators_compatible(o, a) and operators_compatible(o, b))
        ):
            return f"{o.ref} ({o.name or _who(o)}) of another operator lies between them"
    return None


def _split_notes(
    groups: Sequence[Sequence[OsmObject]],
    keys: SeriesKeys,
    objects: Sequence[OsmObject],
    radius_m: float,
) -> set[DissolveNote]:
    """same_operator_nearby notes for the final clusters of one operator that a numbered series
    or a street links within SERIES_GAP_M, or a web page within SITE_PAGE_GAP_M, and
    tenant_building notes (module docstring). One note per pair of clusters, on the closest
    linked members."""
    found: dict[tuple[int, int], tuple[float, str, str, str]] = {}
    owner = {m.ref: i for i, g in enumerate(groups) for m in g}
    page = {m.ref: _web_page(m) for m in objects}
    operator_keys = {m.ref: set(_operator_keys(m)) for m in objects}
    linkable = sorted(
        (m for m in objects if m.kind not in AREA_KINDS and (m.has_operator or page[m.ref])),
        key=lambda o: (o.extent.minlat, ref_key(o.ref)),
    )
    dlat = SITE_PAGE_GAP_M / METERS_PER_DEGREE_LAT
    for i, a in enumerate(linkable):
        for b in linkable[i + 1 :]:
            if b.extent.minlat > a.extent.maxlat + dlat:
                break
            ga, gb = owner[a.ref], owner[b.ref]
            same_page = page[a.ref] is not None and page[a.ref] == page[b.ref]
            same_operator = bool(operator_keys[a.ref] & operator_keys[b.ref])
            if ga == gb or not (same_page or same_operator):
                continue
            gap = a.gap_m(b)
            link: str | None = None
            if same_operator and gap <= SERIES_GAP_M:
                link = _series_link(a, b, groups[ga], groups[gb], keys)
            if link is None and same_page and gap <= SITE_PAGE_GAP_M and operators_compatible(a, b):
                link = f"both give the web page {a.tags['website']!r}"
            if link is None:
                continue
            pair = (min(ga, gb), max(ga, gb))
            first, second = sorted((a, b), key=lambda o: ref_key(o.ref))
            if pair not in found or gap < found[pair][0]:
                found[pair] = (gap, first.ref, second.ref, f"{link}, {gap:,.0f} m apart")
    notes = {
        DissolveNote("same_operator_nearby", (x, y), "", why) for _, x, y, why in found.values()
    }
    return notes | _tenant_notes(groups, objects, radius_m)


def _tenant_notes(
    groups: Sequence[Sequence[OsmObject]], objects: Sequence[OsmObject], radius_m: float
) -> set[DissolveNote]:
    """tenant_building notes: a cluster that is one building with an operator, within
    TENANT_GAP_M of a building of a cluster with at least TENANT_ROW buildings whose operators
    all agree and disagree with the building's, when every object with operator information within
    radius_m of the building agrees with the row's operator (no third operator around)."""
    rows = []
    for g in groups:
        buildings = [m for m in g if m.kind == "building"]
        named = [m for m in g if m.has_operator]
        if len(buildings) >= TENANT_ROW and named and _all_compatible(named):
            rows.append((buildings, named[0]))
    with_op = sorted((o for o in objects if o.has_operator), key=lambda o: o.extent.minlat)
    south_edges = [o.extent.minlat for o in with_op]
    notes: set[DissolveNote] = set()
    for g in groups:
        if len(g) != 1 or g[0].kind != "building" or not g[0].has_operator:
            continue
        lone = g[0]
        for buildings, operator in rows:
            if operators_compatible(lone, operator):
                continue
            gap, nearest = min((lone.gap_m(m), m.ref) for m in buildings)
            if gap > TENANT_GAP_M:
                continue
            around = _within(lone, with_op, south_edges, radius_m)
            if all(operators_compatible(o, operator) for o in around):
                why = (
                    f"{lone.ref} (operator {_who(lone)!r}) is {gap:,.0f} m from {nearest}, one "
                    f"of {len(buildings)} buildings of {_who(operator)!r}, with no other "
                    f"operator within {radius_m:,.0f} m"
                )
                notes.add(DissolveNote("tenant_building", (lone.ref, nearest), "", why))
                break
    return notes


def dissolve(
    objects: Sequence[OsmObject],
    *,
    radius_m: float = DEFAULT_RADIUS_M,
    regions: Mapping[str, str | None] | None = None,
) -> list[Cluster]:
    """Group objects into clusters (see the module docstring and dissolve_with_notes)."""
    return dissolve_with_notes(objects, radius_m=radius_m, regions=regions).clusters


def _area_order(area: OsmObject) -> tuple[float, tuple[int, int, str]]:
    return (area.area_m2(), ref_key(area.ref))


def dissolve_with_notes(
    objects: Sequence[OsmObject],
    *,
    radius_m: float = DEFAULT_RADIUS_M,
    regions: Mapping[str, str | None] | None = None,
) -> DissolveResult:
    """Group objects into clusters (see the module docstring) and say what a reviewer should see
    (DissolveNote). regions (ref -> county FIPS) keeps every cluster inside one region, except
    for containment in an outline. Clusters are sorted by their representative's ref_key, each
    cluster's members by ref_key, and the notes by kind and refs. The clusters hold the objects
    as given; implied operators only steer the joins."""
    if radius_m <= 0:
        raise ValueError(f"radius_m must be positive, not {radius_m}")
    by_ref: dict[str, OsmObject] = {}
    for obj in objects:
        if obj.ref in by_ref:
            raise ValueError(f"duplicate OSM object {obj.ref}")
        by_ref[obj.ref] = obj
    forms = operator_forms(by_ref.values())
    implied = implied_operators(by_ref.values(), forms)
    ordered = sorted(
        (
            replace(o, implied_operator=implied[o.ref]) if o.ref in implied else o
            for o in by_ref.values()
        ),
        key=lambda o: ref_key(o.ref),
    )
    index = {o.ref: i for i, o in enumerate(ordered)}
    uf = _UnionFind(len(ordered))
    notes: set[DissolveNote] = set()

    def other_region(a: OsmObject, b: OsmObject) -> bool:
        return regions is not None and regions.get(a.ref) != regions.get(b.ref)

    def join_rule(rule: str) -> Join:
        def join(a: OsmObject, b: OsmObject) -> None:
            # Every join but containment in an outline keeps a set in one region.
            if other_region(a, b):
                pair = tuple(sorted((a.ref, b.ref), key=ref_key))
                notes.add(DissolveNote("county_blocked", (pair[0], pair[1]), rule))
            else:
                uf.union(index[a.ref], index[b.ref])

        return join

    def join_area(obj: OsmObject, area: OsmObject) -> None:
        if not other_region(obj, area):
            uf.union(index[obj.ref], index[area.ref])
        elif area.outline:
            # The outline holds the object: one site across a county line (Microsoft TRP3 in the
            # Texas Research Park Campus, Google NBY-4 in its New Albany site).
            uf.union(index[obj.ref], index[area.ref])
            notes.add(DissolveNote("county_line", (obj.ref, area.ref), "1"))
        else:
            notes.add(DissolveNote("county_blocked", (obj.ref, area.ref), "1"))

    campuses = [o for o in ordered if o.kind == "campus"]
    sites = [o for o in ordered if o.kind == "site"]
    others = [o for o in ordered if o.kind not in AREA_KINDS]

    # Rule 1: containment in a campus, and of anything but a site in a site. An area without an
    # operator whose contents disagree holds only the objects at its address.
    def covered(area: OsmObject, pool: Sequence[OsmObject]) -> list[OsmObject]:
        return [
            o
            for o in pool
            if o.ref != area.ref and area.covers(o.lat, o.lon) and operators_compatible(o, area)
        ]

    held_by: dict[str, set[str]] = {}  # a mixed area -> the refs it holds
    for area, pool in [(c, others) for c in campuses] + [(s, others + campuses) for s in sites]:
        inside = covered(area, pool)
        if not area.has_operator and not _all_compatible(inside):
            anchors = mixed_anchors(area, inside)
            kept = {
                o.ref
                for o in inside
                if anchors and all(operators_compatible(o, a) for a in anchors)
            }
            held_by[area.ref] = kept
            for o in inside:
                if o.ref not in kept:
                    notes.add(DissolveNote("mixed_operators", (o.ref, area.ref), "1"))

    def holds(area: OsmObject, obj: OsmObject) -> bool:
        return area.ref not in held_by or obj.ref in held_by[area.ref]

    for areas, pool in ((campuses, others), (sites, others + campuses)):
        for obj in pool:
            hits = [a for a in covered_by(obj, areas) if holds(a, obj)]
            if hits:
                join_area(obj, min(hits, key=_area_order))

    # An object at an area's own street address whose operator disagrees with the area's.
    for area in campuses + sites:
        if not area.addresses:
            continue
        for obj in others:
            if (
                obj.ref != area.ref
                and shares_address(obj, area)
                and area.covers(obj.lat, obj.lon)
                and not operators_compatible(obj, area)
            ):
                notes.add(DissolveNote("address_conflict", (obj.ref, area.ref), "1"))

    # Rule 2: same operator (name or Wikidata id, or the operator a name implies), extents within
    # radius_m.
    by_operator: dict[str, list[OsmObject]] = {}
    for obj in others:
        for okey in _operator_keys(obj):
            by_operator.setdefault(okey, []).append(obj)
    _join_close(join_rule("2"), by_operator.values(), radius_m)

    # Rule 2 between areas of one operator that adjoin: a campus and its expansion site, or two
    # outers of one site; two that list different street addresses are two projects (a note).
    by_area_operator: dict[str, list[OsmObject]] = {}
    for area in campuses + sites:
        if area.container:
            continue
        for okey in _operator_keys(area):
            by_area_operator.setdefault(okey, []).append(area)
    join_areas = join_rule("2 (areas)")

    def join_or_note(a: OsmObject, b: OsmObject) -> None:
        gap = adjoining_gap_m(a, b)
        if gap is None or uf.find(index[a.ref]) == uf.find(index[b.ref]):
            return
        if a.addresses and b.addresses and not a.addresses & b.addresses:
            first, second = sorted((a, b), key=lambda o: ref_key(o.ref))
            why = (
                f"two site polygons of one operator {gap:,.0f} m apart that list different "
                "street addresses"
            )
            notes.add(DissolveNote("same_operator_nearby", (first.ref, second.ref), "2", why))
        else:
            join_areas(a, b)

    _join_close(join_or_note, by_area_operator.values(), ADJOIN_M)

    # Rule 3: objects without operator information, by name (or both unnamed) within
    # NEIGHBOR_GAP_M, by street address within radius_m, and unnamed large halls within
    # LARGE_HALL_GAP_M.
    by_name: dict[str, list[OsmObject]] = {}
    by_address: dict[tuple[str, str], list[OsmObject]] = {}
    halls: list[OsmObject] = []
    for obj in others:
        if obj.has_operator:
            continue
        by_name.setdefault(normalize_name(obj.name) if obj.name else "", []).append(obj)
        if obj.address_key is not None:
            by_address.setdefault(obj.address_key, []).append(obj)
        if not obj.name and obj.area_m2() >= LARGE_HALL_M2:
            halls.append(obj)
    join3 = join_rule("3")
    _join_close(join3, by_name.values(), NEIGHBOR_GAP_M)
    _join_close(join3, by_address.values(), radius_m)
    _join_close(join3, [halls], LARGE_HALL_GAP_M)

    # Rule 4: the same specific name, compatible operators, extents within NAME_GAP_M; sibling
    # names within radius_m. The operators of both clusters must agree, so an object without an
    # operator never bridges two operators' clusters.
    words = operator_words(ordered)
    by_specific: dict[str, list[OsmObject]] = {}
    by_stem: dict[str, list[OsmObject]] = {}
    for obj in ordered:
        key = specific_name(obj.name, words)
        if key is not None:
            by_specific.setdefault(key, []).append(obj)
        elif obj.name and obj.has_operator:
            # A name that only names the operator ("QTS", "Compass Data Center") says which site
            # it means when the operator is the same too.
            who = obj.operator_key or f"qid:{obj.operator_qid}"
            by_specific.setdefault(f"{normalize_name(obj.name)}\x00{who}", []).append(obj)
        stem = sibling_stem(obj.name, forms)
        if stem is not None:
            by_stem.setdefault(stem, []).append(obj)
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

    def named_join(rule: str) -> Join:
        join = join_rule(rule)

        def join_named(a: OsmObject, b: OsmObject) -> None:
            ra, rb = uf.find(index[a.ref]), uf.find(index[b.ref])
            join(a, b)
            root = uf.find(index[a.ref])
            if ra != rb and root == uf.find(index[b.ref]):
                with_operator[root] = with_operator.pop(ra, []) + with_operator.pop(rb, [])

        return join_named

    _join_close(named_join("4"), by_specific.values(), NAME_GAP_M, agree)
    _join_close(named_join("4 (sibling names)"), by_stem.values(), radius_m, agree)

    # Rule 5: an unnamed object without an operator and one with an operator at the same address.
    join5 = join_rule("5")
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
                        join5(obj, other)

    # Rule 6: a node inside a building's bounding box.
    join6 = join_rule("6")
    points = [o for o in others if o.kind == "point"]
    for building in others:
        if building.kind != "building" or building.bounds is None:
            continue
        inside = [p for p in points if building.bounds.contains(p.lat, p.lon)]
        if inside and _all_compatible([building, *inside]):
            for p in inside:
                join6(p, building)

    def members_by_root() -> dict[int, list[OsmObject]]:
        out: dict[int, list[OsmObject]] = {}
        for obj in ordered:
            out.setdefault(uf.find(index[obj.ref]), []).append(obj)
        return out

    # Rule 7: an unnamed building without operator information in one operator's row of
    # buildings (a node marks where a tenant is, not a row).
    join7 = join_rule("7")
    with_op = sorted((o for o in others if o.has_operator), key=lambda o: o.extent.minlat)
    south_edges = [o.extent.minlat for o in with_op]
    groups_now = members_by_root()
    for obj in others:
        if obj.kind != "building" or obj.has_operator or obj.name:
            continue
        root = uf.find(index[obj.ref])
        if any(m.has_operator for m in groups_now[root]):
            continue
        near = _within(obj, with_op, south_edges, radius_m)
        close = [o for o in near if o.kind == "building" and obj.gap_m(o) <= NEIGHBOR_GAP_M]
        drawn_over = any(
            o.bounds is not None
            and obj.bounds is not None
            and box_overlap(obj.bounds, o.bounds) >= DUPLICATE_OVERLAP
            for o in close
        )  # the same building mapped twice: a held duplicate, not a building of the row
        if close and not drawn_over and _all_compatible(near):
            join7(obj, min(close, key=lambda o: (obj.gap_m(o), ref_key(o.ref))))

    # Rule 8: a numbered sibling or a street neighbour of one operator just beyond radius_m.
    join8 = join_rule("8")
    series = {o.ref: series_key(o.name, forms) for o in ordered}
    pairs8: list[tuple[float, OsmObject, OsmObject]] = []
    for same in by_operator.values():
        swept = sorted(same, key=lambda o: (o.extent.minlat, ref_key(o.ref)))
        dlat = NAME_GAP_M / METERS_PER_DEGREE_LAT
        for i, a in enumerate(swept):
            for b in swept[i + 1 :]:
                if b.extent.minlat > a.extent.maxlat + dlat:
                    break
                gap = a.gap_m(b)
                if radius_m < gap <= NAME_GAP_M:
                    first, second = sorted((a, b), key=lambda o: ref_key(o.ref))
                    pairs8.append((gap, first, second))
    pairs8.sort(key=lambda p: (p[0], ref_key(p[1].ref), ref_key(p[2].ref)))
    groups_now = members_by_root()
    for gap, a, b in pairs8:
        ra, rb = uf.find(index[a.ref]), uf.find(index[b.ref])
        if ra == rb:
            continue
        group_a, group_b = groups_now[ra], groups_now[rb]
        link = _series_link(a, b, group_a, group_b, series)
        if link is None or not _all_compatible([m for m in group_a + group_b if m.has_operator]):
            continue
        apart = _separation(a, b, group_a, group_b, ordered)
        if apart is not None:
            why = f"{link}, {gap:,.0f} m apart, but {apart}"
            notes.add(DissolveNote("same_operator_nearby", (a.ref, b.ref), "8", why))
            continue
        join8(a, b)
        root = uf.find(index[a.ref])
        if root == uf.find(index[b.ref]):  # not blocked by a county line
            groups_now.pop(ra, None)
            groups_now.pop(rb, None)
            groups_now[root] = group_a + group_b

    grouped = members_by_root()
    root_of = {m.ref: root for root, members in grouped.items() for m in members}
    noted = {
        frozenset(root_of[r] for r in n.refs) for n in notes if n.kind == "same_operator_nearby"
    }
    for note in sorted(
        _split_notes(list(grouped.values()), series, ordered, radius_m),
        key=lambda n: (n.kind, ref_key(n.refs[0]), ref_key(n.refs[1])),
    ):
        pair = frozenset(root_of[r] for r in note.refs)
        if note.kind == "tenant_building" or pair not in noted:
            notes.add(note)
            noted.add(pair)
    grouped = {root: [by_ref[m.ref] for m in members] for root, members in grouped.items()}
    clusters = [
        Cluster(members=tuple(members), representative=choose_representative(members))
        for members in grouped.values()
    ]
    return DissolveResult(
        clusters=sorted(clusters, key=lambda c: ref_key(c.representative.ref)),
        notes=sorted(notes, key=lambda n: (n.kind, ref_key(n.refs[0]), ref_key(n.refs[1]), n.rule)),
    )


def covered_by(obj: OsmObject, areas: Sequence[OsmObject]) -> list[OsmObject]:
    """The areas whose outline (or box + 30 m) holds obj's center and whose operators agree with
    obj's (rule 1)."""
    return [
        a
        for a in areas
        if a.ref != obj.ref and a.covers(obj.lat, obj.lon) and operators_compatible(obj, a)
    ]
