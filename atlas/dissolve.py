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
   "Compass Data Center").
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

An element tagged with another primary use (amenity, shop, tourism, healthcare: the post office
in the Terminal Annex) or whose operator tag names a general contractor (HITT on a construction
site) has no operator for these rules: its operator tag is not the data center's.

When regions is given (ref -> county FIPS), no rule joins two objects in different regions,
except containment in an outline (a campus inside one site polygon is one facility across a
county line); such a join and every join the regions block are DissolveNotes. Distances are gaps
between bounding boxes, not between centers, so two large buildings that touch join although
their centers are far apart. Two campus objects join each other directly only by name (rule 4) or
inside one site. The result does not depend on input order.
"""

from __future__ import annotations

import math
import re
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


NoteKind = Literal["county_line", "county_blocked", "mixed_operators", "address_conflict"]


@dataclass(frozen=True)
class DissolveNote:
    """What dissolve() did or refused that a reviewer should see. refs is (object, other):

    - county_line: object joined the area `other` (its outline holds it) across a county line;
    - county_blocked: a rule would have joined the two but for a county line;
    - mixed_operators: the area `other` has no operator and holds objects whose operators
      disagree, so it held only those at its address, and object is one it did not hold;
    - address_conflict: object lies in the area `other` and shares its street address, but their
      operators disagree.
    """

    kind: NoteKind
    refs: tuple[str, str]
    rule: str = ""


@dataclass(frozen=True)
class DissolveResult:
    clusters: list[Cluster]
    notes: list[DissolveNote]


def choose_representative(members: Sequence[OsmObject]) -> OsmObject:
    """The campus or site object (the largest if several; a container only when the cluster has
    no other), else the largest way or relation by bounding box, else the node with the lowest
    id."""
    campuses = [m for m in members if m.kind in AREA_KINDS and not m.container]
    campuses = campuses or [m for m in members if m.kind in AREA_KINDS]
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
        if obj.operator_key is not None:
            by_operator.setdefault("name:" + operator_group(obj.operator_key), []).append(obj)
        if obj.operator_qid is not None:
            by_operator.setdefault("qid:" + obj.operator_qid, []).append(obj)
    _join_close(join_rule("2"), by_operator.values(), radius_m)

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

    grouped: dict[int, list[OsmObject]] = {}
    for obj in ordered:
        grouped.setdefault(uf.find(index[obj.ref]), []).append(by_ref[obj.ref])
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
