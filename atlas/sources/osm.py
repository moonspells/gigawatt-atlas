"""OpenStreetMap seed importer: data centers from Overpass, dissolved into campuses and cross-checked
with PNNL IM3 (07 §4.1, §4.2, §5.1, §6.5 step 1). docs/sources/osm-pnnl.md describes the mapping.

1. Fetch: one Overpass query (OVERPASS_QUERY: the data center objects, then the site polygons
   with their outlines) POSTed to each endpoint in turn until one answers with a complete result.
   Overpass can answer HTTP 200 with a partial result, so a `remark` with "runtime error", a
   missing osm3s.timestamp_osm_base or fewer than MIN_ELEMENTS elements also counts as a failure.
   `--input FILE` reads a saved response instead (no element minimum).
2. Parse every element into an OsmObject (center = node position or bounding-box midpoint).
3. Dissolve objects into campuses (atlas.dissolve), each within one county.
4. Join PNNL rows to the clusters (atlas.sources.pnnl) unless --no-pnnl.
5. Build one campus record per cluster, with external_ids["osm"] = every member ref, unless the
   cluster is held for review (_hold). OSM dates no status (`other` events), and the scope screen
   keeps what may not be a data center out of scope for a reviewer (scope_doubt). The record's
   city is the Census place that contains its point (atlas.geo.places, ImportContext.places()),
   never addr:city, the postal city.

Data is © OpenStreetMap contributors, ODbL 1.0; PNNL IM3 is ODbL 1.0 (07 §5.1).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import HttpUrl, JsonValue, ValidationError

from atlas.crosswalk import Crosswalked, from_osm_tags
from atlas.dissolve import (
    AREA_KINDS,
    DEFAULT_RADIUS_M,
    Bounds,
    Cluster,
    ObjectKind,
    OsmObject,
    Polyline,
    dissolve,
    operator_conflicts,
    ref_key,
)
from atlas.geo.fips import IN_SCOPE
from atlas.net import FetchError, FetchResult, fetch
from atlas.schema.record import (
    PLACEHOLDER_ID,
    Alias,
    Building,
    Capacity,
    DatePrecision,
    FacilityRecord,
    FieldMeta,
    FuzzyDate,
    Location,
    OrgRef,
    Parties,
    Phase,
    Review,
    Site,
    Source,
    StatusEvent,
)
from atlas.schema.rollup import ACTIVE_ORDER, apply_rollup, period_start
from atlas.sources import pnnl
from atlas.sources.base import (
    Candidate,
    ImportContext,
    ImportResult,
    InputSnapshot,
    ReviewItem,
    load_input,
)
from atlas.text import clean_text, normalize_name, normalize_org
from atlas.validate import EARLIEST_DATE, FUTURE_YEARS, add_years

if TYPE_CHECKING:
    from atlas.geo.counties import County, CountyIndex
    from atlas.geo.places import PlaceIndex
    from atlas.geocode import Gazetteer

__all__ = [
    "IMPORTER",
    "MIN_ELEMENTS",
    "OSM_SUPPORTS",
    "OVERPASS_ENDPOINTS",
    "OVERPASS_QUERY",
    "BuildResult",
    "OsmImporter",
    "OsmObject",
    "OverpassError",
    "OverpassResult",
    "build_candidates",
    "check_overpass",
    "classify",
    "fetch_overpass",
    "osm_date",
    "osm_status",
    "parse_mw",
    "parse_overpass",
    "scope_doubt",
    "telecom_site",
]

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
# `out tags bb` (not the plan's `out center tags`): campus containment needs the extents, and the
# center is the bounding-box midpoint, which is what `center` returns. The second statement adds
# the site polygons (landuse=industrial or construction with industrial=data_centre, 172 US ways
# and relations on 2026-10-09) with their outlines (`out geom`: a way's nodes, a relation's member
# ways; about 250 kB), so that the buildings inside a site join it. An element in both sets comes
# twice and is read once.
OVERPASS_QUERY = """[out:json][timeout:180];
area["ISO3166-1"="US"][admin_level=2]->.us;
(
  nwr["telecom"="data_center"](area.us);
  nwr["building"="data_center"](area.us);
  nwr["construction:telecom"="data_center"](area.us);
  nwr["proposed:telecom"="data_center"](area.us);
  nwr["construction"="data_center"](area.us);
);
out tags bb;
(
  way["industrial"="data_centre"](area.us);
  way["industrial"="data_center"](area.us);
  relation["industrial"="data_centre"](area.us);
  relation["industrial"="data_center"](area.us);
);
out geom;"""
MIN_ELEMENTS = 1_000  # the US result had 1,871-1,886 elements on 2026-10-07; fewer means partial
OVERPASS_TIMEOUT_S = 240.0
OVERPASS_MAX_BYTES = 50_000_000
OVERPASS_RETRIES = 1  # per endpoint; then the next endpoint is tried
OSM_LICENSE = "ODbL-1.0"
OSM_PUBLISHER = "OpenStreetMap contributors"
OSM_SUPPORTS = (
    "/canonical_name",
    "/aliases",
    "/parties",
    "/location",
    "/buildings",
    "/capacity",
    "/status_history/0",
)
OSM_SOURCE_ID = "s1"
PNNL_SOURCE_ID = "s2"
STATUS_CONFIDENCE = 0.60
CAPACITY_CONFIDENCE = 0.70
COUNTY_CONFIDENCE = 0.90
COORD_DECIMALS = 7
POWER_TAGS = (("it_power", "it_mw"), ("input:electricity", "facility_mw"))
_PHASE_LABEL = {
    "operating": "Operating",
    "under_construction": "Under construction",
    "announced": "Announced",
}
_POWER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(gw|mw|kw)?$", re.IGNORECASE)
_POWER_FACTOR = {"gw": 1000.0, "mw": 1.0, "kw": 0.001}
_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
_DATE_PRECISION: dict[int, DatePrecision] = {4: "year", 7: "month", 10: "day"}
_MAX_MW = 10_000.0
# 07 §2.2 puts telecom central offices and edge sites out of scope. OSM telecom=data_center also
# tags cable landing stations and telephone company offices, whose names or operators say so.
_TELECOM_RE = re.compile(
    r"\b(?:cable landing|landing station|central office|wire center"
    r"|telephone (?:co|coop|cooperative|company)|cooperative telephone)\b",
    re.IGNORECASE,
)
_TELECOM_TAGS = frozenset({"exchange", "central_office"})  # telecom=* on a building=data_center
# scope_doubt: what else OSM tags telecom=data_center that 07 §2.2 may not count as a data center.
_TEXT_KEYS = ("name", "alt_name", "official_name", "short_name", "description", "note", "fixme")
_CRYPTO_RE = re.compile(
    r"\b(?:crypto|cryptocurrenc\w*|cryptomin\w*|bitcoin\w*|blockchain|btc|mining|miners?)\b",
    re.IGNORECASE,
)
_CRYPTO_INDUSTRIAL = frozenset({"mine", "cryptocurrency_mine", "bitcoin_mine"})  # industrial=*
_CRYPTO_VALUES = frozenset({"crypto", "cryptocurrency", "bitcoin"})  # data_centre=*
_ROOM_RE = re.compile(
    r"\b(?:computer|server|machine|equipment|network) (?:rooms?|labs?|closets?)\b"
    r"|\bcomputer labs?\b|\bdata labs?\b|\bclassrooms?\b"
    r"|\bsomewhere (?:on|in) this (?:floor|building)\b"
    r"|\btape vaults?\b|\bvital records\b|\brecords (?:center|centre|management|storage|vault)s?\b",
    re.IGNORECASE,
)
_OUTBUILDINGS = frozenset({"shed", "hut", "cabin", "kiosk", "container", "garage", "carport"})
_MIN_FOOTPRINT_M2 = 200.0
_DATA_CENTER_RE = re.compile(
    r"data ?cent(?:er|re)s?|\bcolo(?:cation)?\b|\bhosting\b|\bcloud\b|\bservers?\b"
    r"|\bcomput(?:e|er|ers|ing|ation)\b|supercomput|\bhpc\b|\bdc ?-?\d|\bnap\b|carrier hotel"
    r"|internet exchange|\bidc\b",
    re.IGNORECASE,
)
# normalize_name of names that are only a telephone company's brand (2026-10-08: "CenturyLink",
# "Verizon", "AT&T", "Windstream"): telephone exchanges and offices as often as data centers.
_TELEPHONE_BRANDS = frozenset(
    {
        "at t",
        "brightspeed",
        "centurylink",
        "cincinnati bell",
        "consolidated communications",
        "embarq",
        "frontier",
        "frontier communications",
        "level 3",
        "level 3 communications",
        "lumos networks",
        "qwest",
        "tds telecom",
        "verizon",
        "verizon wireless",
        "windstream",
        "windstream communications",
    }
)
_GOVERNMENT_RE = re.compile(r"^(?:city|county|town|village|borough|township) of\b", re.IGNORECASE)
_NOTE_KEYS = ("note", "fixme", "FIXME", "description")
_APPROXIMATE_RE = re.compile(r"approximat", re.IGNORECASE)
DUPLICATE_OVERLAP = 0.5  # share of the smaller box: an unnamed way drawn over another building
DUPLICATE_NAME_GAP_M = 1_000.0  # two records with the same canonical name this close: review


class OverpassError(ValueError):
    """An Overpass response that is not a complete result."""


# ---------------------------------------------------------------------------- parse


def _clean(value: object) -> str:
    return clean_text(str(value))


def check_overpass(doc: object, *, min_elements: int = MIN_ELEMENTS) -> str:
    """Raise OverpassError unless doc is a complete Overpass JSON result; return timestamp_osm_base."""
    if not isinstance(doc, dict):
        raise OverpassError("the response is not a JSON object")
    remark = doc.get("remark")
    if isinstance(remark, str) and "runtime error" in remark.casefold():
        raise OverpassError(f"remark: {remark.strip()[:200]}")
    osm3s = doc.get("osm3s")
    base = osm3s.get("timestamp_osm_base") if isinstance(osm3s, dict) else None
    if not isinstance(base, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", base):
        raise OverpassError("osm3s.timestamp_osm_base is missing")
    elements = doc.get("elements")
    if not isinstance(elements, list):
        raise OverpassError("no elements array")
    if len(elements) < min_elements:
        raise OverpassError(f"{len(elements)} elements, fewer than {min_elements}")
    return base


# A building that is not built yet carries its building tag under a lifecycle prefix
# (proposed:building=industrial, construction:building=yes), as a value other than "no".
LIFECYCLE_BUILDING_KEYS = ("proposed:building", "construction:building")
# Tags that make an area without a building tag a data center campus.
CAMPUS_KEYS = ("telecom", "construction:telecom", "proposed:telecom", "construction")
SITE_VALUES = frozenset({"data_centre", "data_center"})  # industrial=*


def classify(osm_type: str, tags: Mapping[str, str]) -> ObjectKind:
    """campus: a way or relation with telecom, construction:telecom, proposed:telecom or
    construction = data_center and no building tag (a lifecycle one, proposed:building=* or
    construction:building=*, counts as a building tag), or with building=no; site: any other way or
    relation without a building tag that is tagged industrial=data_centre (or data_center);
    building: any other way or relation; point: a node.

    A planned building drawn as its own polygon is one building of a site, not a campus: as a
    campus, each of Rowan Green's four proposed:building=industrial halls was a record of its own,
    and QTS Hillsboro 3's two polygons split the site (2026-10-07). A construction site drawn as
    landuse=construction + construction=data_center is a campus, so the halls being built on it
    join it ("AWS IAD-500 and IAD-501", way 1319699416, was a record of its own next to them).
    """
    if osm_type == "node":
        return "point"
    if tags.get("building") == "no":
        return "campus"
    is_building = "building" in tags or any(
        tags.get(key, "no") != "no" for key in LIFECYCLE_BUILDING_KEYS
    )
    if is_building:
        return "building"
    if any(tags.get(key) == "data_center" for key in CAMPUS_KEYS):
        return "campus"
    if tags.get("industrial") in SITE_VALUES:
        return "site"
    return "building"


def _outline(element: Mapping[str, Any]) -> tuple[Polyline, ...] | None:
    """A way's `out geom` ring (closed), or a relation's member ways as they come; None without
    geometry."""

    def line(points: object) -> Polyline:
        if not isinstance(points, list):
            return ()
        return tuple(
            (float(p["lat"]), float(p["lon"]))
            for p in points
            if isinstance(p, dict) and "lat" in p and "lon" in p
        )

    if element.get("type") == "way":
        ring = line(element.get("geometry"))
        if len(ring) < 3:
            return None
        return (ring if ring[0] == ring[-1] else (*ring, ring[0]),)
    members = element.get("members")
    if not isinstance(members, list):
        return None
    lines = [
        line(m.get("geometry"))
        for m in members
        if isinstance(m, dict)
        and m.get("type") == "way"
        and m.get("role") in ("outer", "inner", "")
    ]
    lines = [x for x in lines if len(x) >= 2]
    return tuple(lines) or None


def parse_overpass(doc: Mapping[str, Any]) -> tuple[list[OsmObject], list[str]]:
    """OsmObjects from an Overpass `out tags bb` (or `out center`, or `out geom`) result, and the
    refs of elements that had no position. An element that comes twice (in both statements of
    OVERPASS_QUERY) is read once, with the outline of the copy that has one."""
    objects: list[OsmObject] = []
    skipped: list[str] = []
    seen: dict[str, int] = {}
    for element in doc.get("elements", []):
        if not isinstance(element, dict):
            continue
        osm_type, osm_id = element.get("type"), element.get("id")
        if osm_type not in ("node", "way", "relation") or not isinstance(osm_id, int):
            continue
        ref = f"{osm_type}/{osm_id}"
        outline = _outline(element) if osm_type != "node" else None
        if ref in seen:
            at = seen[ref]
            if at >= 0 and outline is not None and objects[at].outline is None:
                objects[at] = dataclasses.replace(objects[at], outline=outline)
            continue
        seen[ref] = len(objects)
        raw_tags = element.get("tags") or {}
        tags = {str(k): _clean(v) for k, v in raw_tags.items() if _clean(v)}
        bounds: Bounds | None = None
        if osm_type == "node" and "lat" in element and "lon" in element:
            lat, lon = float(element["lat"]), float(element["lon"])
        elif isinstance(element.get("bounds"), dict):
            b = element["bounds"]
            bounds = Bounds(
                float(b["minlat"]), float(b["minlon"]), float(b["maxlat"]), float(b["maxlon"])
            )
            lat, lon = bounds.center
        elif isinstance(element.get("center"), dict):
            lat, lon = float(element["center"]["lat"]), float(element["center"]["lon"])
        else:
            seen[ref] = -1
            skipped.append(ref)
            continue
        kind = classify(osm_type, tags)
        objects.append(OsmObject(ref, lat, lon, bounds, tags, kind, outline))
    return objects, skipped


def parse_mw(value: str) -> float | None:
    """MW from "N MW" (also kW, GW and a bare number, read as MW); None if unparseable or outside
    (0, 10000]."""
    match = _POWER_RE.match(value.strip())
    if match is None:
        return None
    mw = float(match.group(1)) * _POWER_FACTOR[(match.group(2) or "mw").lower()]
    return round(mw, 3) if 0 < mw <= _MAX_MW else None


def osm_date(value: str | None, *, latest: date) -> FuzzyDate | None:
    """A FuzzyDate from an OSM YYYY, YYYY-MM or YYYY-MM-DD value between 1990-01-01 and latest."""
    if not value or not _DATE_RE.match(value):
        return None
    try:
        fuzzy = FuzzyDate(value=value, precision=_DATE_PRECISION[len(value)])
    except ValidationError:
        return None
    return fuzzy if EARLIEST_DATE <= period_start(fuzzy) <= latest else None


# ---------------------------------------------------------------------------- fetch


@dataclass
class OverpassResult:
    doc: dict[str, Any]
    snapshot: InputSnapshot


def fetch_overpass(
    ctx: ImportContext,
    endpoints: Sequence[str],
    *,
    min_elements: int = MIN_ELEMENTS,
    retries: int = OVERPASS_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> OverpassResult:
    """The Overpass result from --input, or from the first endpoint that answers completely."""
    if not endpoints:
        raise ValueError("no Overpass endpoint")
    used: list[str] = []
    parsed: list[dict[str, Any]] = []

    def fetch_any() -> FetchResult:
        failures: list[str] = []
        for url in endpoints:
            try:
                result = fetch(
                    ctx.http,
                    url,
                    method="POST",
                    data={"data": OVERPASS_QUERY},
                    robots=False,
                    timeout=OVERPASS_TIMEOUT_S,
                    max_bytes=OVERPASS_MAX_BYTES,
                    allowed_types=("application/json",),
                    min_interval=0.0,
                    retries=retries,
                    sleep=sleep,
                )
                if result.status != 200:
                    raise OverpassError(f"HTTP {result.status}")
                doc = json.loads(result.content)
                check_overpass(doc, min_elements=min_elements)
            except Exception as e:  # any failure moves on to the next endpoint
                failures.append(f"{url}: {type(e).__name__}: {e}")
                print(f"overpass: {url} failed ({type(e).__name__}: {e})", file=sys.stderr)
                continue
            used.append(url)
            parsed.append(doc)
            return result
        raise FetchError("every Overpass endpoint failed: " + " | ".join(failures))

    data, snapshot = load_input(
        ctx, name="overpass", url=endpoints[0], license=OSM_LICENSE, ext="json", fetch=fetch_any
    )
    # With --input the file is a saved response: the element minimum guards live answers only.
    doc = parsed[0] if parsed else json.loads(data)
    base = check_overpass(doc, min_elements=0)
    snapshot = snapshot.model_copy(
        update={"url": HttpUrl(used[0] if used else endpoints[0]), "upstream_version": base}
    )
    return OverpassResult(doc=doc, snapshot=snapshot)


# ---------------------------------------------------------------------------- records


def _most_common(values: Iterable[str | None]) -> str | None:
    """The most frequent non-empty value; ties go to the first one seen."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def _only(values: Iterable[str | None]) -> str | None:
    """The one non-empty value when every member that has a value agrees, else None."""
    found = {v for v in values if v}
    return next(iter(found)) if len(found) == 1 else None


def _street(m: OsmObject) -> str | None:
    street = m.tags.get("addr:street")
    if not street:
        return None
    number = m.tags.get("addr:housenumber")
    return f"{number} {street}" if number else street


def _pick(
    rep: OsmObject, ordered: Sequence[OsmObject], get: Callable[[OsmObject], str | None]
) -> str | None:
    """The representative's value, else the value every member that has one agrees on. A value
    that only some members share would put the representative's name at another building's
    address (QTS Manassas DC5 at DC1's 9400 Godwin Drive, 2026-10-08)."""
    return get(rep) or _only(get(m) for m in ordered)


def osm_status(tags: Mapping[str, str]) -> Crosswalked | None:
    """crosswalk.from_osm_tags, which reads OSM's proposed tags as announced (no OSM tag is a
    filing, 07 §2.3) and writes every status as an `other` observation."""
    return from_osm_tags(tags)


@dataclass
class _Context:
    """What every cluster's record shares."""

    counties: CountyIndex
    gazetteer: Gazetteer  # county full names ("Taylor County", "Manassas city") for names
    now: datetime
    as_of: FuzzyDate  # the snapshot date (timestamp_osm_base), day precision
    as_of_date: date
    latest_date: date  # as_of_date + 15 years: the latest date validation accepts
    osm_retrieved_at: datetime
    pnnl_join: pnnl.JoinResult | None
    pnnl_snapshot: InputSnapshot | None
    existing: Mapping[str, FacilityRecord]
    existing_by_ref: Mapping[str, set[str]]
    operators: frozenset[str]  # normalize_org of every operator and operator:short in the input


@dataclass
class BuildResult:
    candidates: list[Candidate]
    review: list[ReviewItem]
    metrics: dict[str, float | int]


def _review(
    kind: str,
    reason: str,
    *,
    external_id: str | None,
    record_id: str | None = None,
    data: dict[str, JsonValue] | None = None,
    source: str = "osm",
) -> ReviewItem:
    return ReviewItem(
        source=source,
        kind=kind,
        external_id=external_id,
        record_id=record_id,
        reason=reason,
        data=data or {},
    )


def _existing_id(cluster: Cluster, ctx: _Context) -> str | None:
    """The stored record that shares this cluster's OSM refs, when exactly one does."""
    ids: set[str] = set()
    for ref in cluster.refs:
        ids |= ctx.existing_by_ref.get(ref, set())
    return next(iter(ids)) if len(ids) == 1 else None


@dataclass(frozen=True)
class _Group:
    """Members that share a crosswalked status; status is that of the first member."""

    status: Crosswalked
    members: tuple[OsmObject, ...]


def _status_groups(ordered: Sequence[OsmObject]) -> list[_Group]:
    """Members grouped by crosswalked status (osm_status), most advanced first (operating,
    under_construction, announced). Members are kept in `ordered` order, so the representative
    leads its group."""
    by_status: dict[str, list[tuple[Crosswalked, OsmObject]]] = {}
    for m in ordered:
        found = osm_status(m.tags)
        if found is not None:
            by_status.setdefault(found.status, []).append((found, m))
    ordered_groups = sorted(
        by_status.values(), key=lambda items: ACTIVE_ORDER.index(items[0][0].status), reverse=True
    )
    return [_Group(items[0][0], tuple(m for _, m in items)) for items in ordered_groups]


def _capacity(
    cluster: Cluster, ordered: Sequence[OsmObject], review: list[ReviewItem]
) -> tuple[Capacity, list[str]]:
    """Capacity from it_power and input:electricity, and the field_meta pointers it sets."""
    values: dict[str, float | None] = {}
    stated: list[str] = []
    for tag, field_name in POWER_TAGS:
        raw: list[str] = []
        parsed: dict[str, float] = {}
        for m in ordered:
            value = m.tags.get(tag)
            if not value:
                continue
            raw.append(f"{m.name or m.ref} {value}")
            mw = parse_mw(value)
            if mw is None:
                review.append(
                    _review(
                        "unit_parse",
                        f"{tag}={value!r} is not a power value in MW, kW or GW within (0, 10000] MW",
                        external_id=m.ref,
                        data={
                            "tag": tag,
                            "value": value,
                            "representative": cluster.representative.ref,
                        },
                    )
                )
            else:
                parsed[m.ref] = mw
        if raw:
            stated.append(f"OSM {tag}: " + "; ".join(raw))
        campuses = [m for m in cluster.members if m.kind == "campus"]
        buildings = [m for m in cluster.members if m.kind not in AREA_KINDS]
        total: float | None = None
        if campuses and all(m.ref in parsed for m in campuses):
            total = sum(parsed[m.ref] for m in campuses)  # a campus-level value covers the site
        elif buildings and all(m.ref in parsed for m in buildings):
            total = sum(parsed[m.ref] for m in buildings)
        values[field_name] = round(total, 3) if total is not None and total <= _MAX_MW else None
    capacity = Capacity(
        it_mw=values["it_mw"],
        facility_mw=values["facility_mw"],
        mw_as_stated=" | ".join(stated) or None,
    )
    pointers = [f"/capacity/{f}" for _, f in POWER_TAGS if values[f] is not None]
    return capacity, pointers


def _start_date(group: _Group, rep: OsmObject, ctx: _Context) -> tuple[FuzzyDate, str] | None:
    """The representative's start_date, else the earliest among the group's members."""
    dated = [
        (d, m.ref)
        for m in group.members
        if (d := osm_date(m.tags.get("start_date"), latest=ctx.as_of_date)) is not None
    ]
    for d, ref in dated:
        if ref == rep.ref:
            return d, ref
    return min(dated, key=lambda x: (period_start(x[0]), ref_key(x[1]))) if dated else None


def _period_after(d: FuzzyDate) -> date:
    """The first day after the year, quarter, month or day d stands for."""
    start = period_start(d)
    if d.precision == "day":
        return start + timedelta(days=1)
    months = {"year": 12, "quarter": 3, "month": 1}[d.precision]
    month = start.month - 1 + months
    return date(start.year + month // 12, month % 12 + 1, 1)


def _phase_name(group: _Group) -> str:
    names = list(dict.fromkeys(m.name or m.ref for m in group.members))
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return f"{_PHASE_LABEL[group.status.status]} in OpenStreetMap: {shown}"


def _events(
    groups: Sequence[_Group], cluster: Cluster, ctx: _Context, review: list[ReviewItem]
) -> tuple[list[StatusEvent], list[Phase], dict[str, str]]:
    """Status events, phases and each member's phase_id.

    One `other` event per status group, from the crosswalk, at the snapshot date: OpenStreetMap
    says what a feature is on the day it was read, not when its status began, so the event sets
    the status and dates nothing (07 §2.3; rollup.derive_dates skips `other` events, as for an AI
    GridWatch stage). A start_date is OSM's date for the feature, often the building's or the
    start of construction rather than the start of operation (Apple Mesa's start_date 2012 is the
    former factory's), so it dates nothing either: it is kept in the note and filed as an
    `unverified_upstream` item for a reviewer to confirm from a citable source. When members
    disagree, each group becomes a phase ("osm-{status}"), so an operating campus with a building
    under construction stays operating and is expanding (07 §2.3). A non-operating group with an
    opening_date adds a planned energized event, unless that date has passed (a `conflict` item).
    """
    rep = cluster.representative
    phased = len(groups) > 1
    events: list[StatusEvent] = []
    phases: list[Phase] = []
    phase_of: dict[str, str] = {}
    phase_ids: list[str | None] = []
    for group in groups:
        status = group.status
        phase_id = f"osm-{status.status}" if phased else None
        phase_ids.append(phase_id)
        if phase_id is not None:
            phases.append(
                Phase(phase_id=phase_id, name=_phase_name(group), source_ids=[OSM_SOURCE_ID])
            )
            phase_of.update(dict.fromkeys((m.ref for m in group.members), phase_id))
        # The note names no date: as_of is the first snapshot that saw the status, and a note
        # that changed every week would rewrite every record.
        note = (
            f"Tagged {status.label} in OpenStreetMap; status per 07 §4.7, seen on the snapshot "
            "date, not the date it began"
        )
        start = _start_date(group, rep, ctx) if status.status == "operating" else None
        if start is not None:
            as_of, ref = start
            note += f"; start_date={as_of.value} on {ref} is not read as the start of operation"
            review.append(
                _review(
                    "unverified_upstream",
                    f"start_date={as_of.value} on {ref}: OpenStreetMap's start_date dates the "
                    "feature (often the building or the start of construction), not the start of "
                    "operation, so it sets no date; confirm it from a citable source",
                    external_id=rep.ref,
                    data={"osm": ref, "start_date": as_of.value, "refs": list(cluster.refs)},
                )
            )
        events.append(
            StatusEvent(
                seq=len(events) + 1,
                status=status.status,
                event="other",
                as_of=ctx.as_of,
                phase_id=phase_id,
                source_ids=[OSM_SOURCE_ID],
                note=note,
            )
        )
    for group, phase_id in zip(groups, phase_ids, strict=True):
        if group.status.status == "operating":
            continue
        raw = _pick(group.members[0], group.members, lambda m: m.tags.get("opening_date"))
        opening = osm_date(raw, latest=ctx.latest_date)
        if opening is None:
            continue
        if _period_after(opening) <= ctx.as_of_date:
            review.append(
                _review(
                    "conflict",
                    f"opening_date={opening.value} has passed, but OpenStreetMap still tags the "
                    f"site {group.status.label} on {ctx.as_of.value}; the date is not used",
                    external_id=rep.ref,
                    data={"opening_date": opening.value, "status": group.status.status},
                )
            )
            continue
        events.append(
            StatusEvent(
                seq=len(events) + 1,
                status="operating",
                event="energized",
                as_of=opening,
                phase_id=phase_id,
                planned=True,
                source_ids=[OSM_SOURCE_ID],
                note=f"opening_date={opening.value} in OpenStreetMap",
            )
        )
    return events, phases, phase_of


_OBSERVATIONS = ("first_reported", "other")  # the events OSM writes for a status it sees


def _keep_first_reported(
    events: list[StatusEvent], existing: FacilityRecord | None
) -> list[StatusEvent]:
    """Keep the earliest stored as_of of an observation (an `other` event, or the
    `first_reported` event that earlier versions wrote) with the same status and sources, so
    weekly runs never move it forward.

    The phase is not compared: when a cluster gains or loses a status group, its events move
    between phase_id None and "osm-{status}", but the status itself has not changed.
    """
    if existing is None:
        return events
    stored: dict[tuple[str, tuple[str, ...]], FuzzyDate] = {}
    for e in existing.status_history:
        if e.planned or e.event not in _OBSERVATIONS:
            continue
        key = (e.status, tuple(e.source_ids))
        if key not in stored or period_start(e.as_of) < period_start(stored[key]):
            stored[key] = e.as_of
    out: list[StatusEvent] = []
    for e in events:
        old = stored.get((e.status, tuple(e.source_ids)))
        if (
            e.event in _OBSERVATIONS
            and not e.planned
            and old is not None
            and period_start(old) <= period_start(e.as_of)
        ):
            e = e.model_copy(update={"as_of": old.model_copy()})
        out.append(e)
    return out


def _approximate(cluster: Cluster) -> tuple[OsmObject, str] | None:
    """The first member whose note, fixme or description says its position is approximate."""
    for m in cluster.members:
        for key in _NOTE_KEYS:
            text = m.tags.get(key, "")
            if _APPROXIMATE_RE.search(text):
                return m, text
    return None


def _location(
    cluster: Cluster,
    ordered: Sequence[OsmObject],
    county: County,
    lat: float,
    lon: float,
    approximate: bool,
    city: str | None = None,
) -> Location:
    rep = cluster.representative
    shaped = any(m.osm_type in ("way", "relation") for m in cluster.members)
    return Location(
        lat=lat,
        lon=lon,
        precision="footprint" if shaped and not approximate else "site",
        geometry_ref=f"osm:{rep.ref}",
        street=_pick(rep, ordered, _street),
        # The Census place that contains the point (07 §6.5), never addr:city: that is the postal
        # city of the address, which need not contain the point (Norcross for a site in Peachtree
        # Corners). Where the two agree, addr:city is that place.
        city=city,
        postcode=_pick(rep, ordered, lambda m: m.tags.get("addr:postcode")),
        county_name=county.name,
        county_fips=county.fips,
        state_abbr=county.state_abbr,
        geocode_method="osm",
    )


def _starts_with_operator(base: str, operator: str, short: str | None) -> bool:
    """True when base already names the operator: it starts with the operator, its
    operator:short, or the operator's first word (normalized, on word boundaries)."""
    words = normalize_name(base).split()
    for candidate in (operator, short):
        tokens = normalize_name(candidate).split() if candidate else []
        if tokens and words[: len(tokens)] == tokens:
            return True
    first = normalize_name(operator).split()[:1]
    return bool(first) and words[:1] == first


def _canonical_name(
    rep: OsmObject, ordered: Sequence[OsmObject], operator: str | None, place: str, st: str
) -> tuple[str, str | None]:
    """(canonical_name, the member name used as its base)."""
    base_name = rep.name or _most_common(m.name for m in ordered)
    base = base_name or (f"{operator} data center" if operator else "Data center")
    short = rep.tags.get("operator:short") or _most_common(
        m.tags.get("operator:short") for m in ordered if m.operator == operator
    )
    if operator and not _starts_with_operator(base, operator, short):
        base = f"{operator} {base}"
    return f"{base} ({place}, {st})", base_name


def telecom_site(cluster: Cluster) -> str | None:
    """Why the cluster is a telecom site rather than a data center (07 §2.2), or None.

    It is one when every member that has a name, alt_name or operator names a cable landing
    station, a central office, a wire center or a telephone company or cooperative, or is tagged
    telecom=exchange; unnamed members do not count either way.
    """
    reasons: list[str] = []
    for m in cluster.members:
        text = " ".join(v for v in (m.name, m.tags.get("alt_name"), m.operator) if v)
        found = _TELECOM_RE.search(text)
        if m.tags.get("telecom") in _TELECOM_TAGS:
            reasons.append(f"{m.ref} is tagged telecom={m.tags['telecom']}")
        elif found is not None:
            reasons.append(f"{m.ref} is named as a telecom site ({found.group(0)!r})")
        elif text:
            return None
    return reasons[0] if reasons else None


def _texts(m: OsmObject, keys: Sequence[str] = _TEXT_KEYS) -> str:
    return " ".join(v for k in keys if (v := m.tags.get(k)))


def _known_operator(name: str, operators: frozenset[str]) -> bool:
    """True when the name starts with an operator named somewhere in the input ("TierPoint
    Milwaukee" for TierPoint), word by word."""
    words = normalize_org(name).split()
    return any(words[: len(op.split())] == op.split() for op in operators if op)


def scope_doubt(cluster: Cluster, operators: frozenset[str]) -> str | None:
    """Why the cluster may not be a data center in the sense of 07 §2.2, or None. Such a cluster
    is kept out of scope with an `out_of_scope` item, so a reviewer decides; the rules catch
    what the 2026-10-08 seed check found imported as operating data centers:

    - a cryptocurrency mine or a power plant: a campus or site member, or every member that has a
      name, operator or note, with crypto, bitcoin or mining words, industrial=mine or
      cryptocurrency_mine, data_centre=crypto, power=plant or a generator:source tag ("Nautilus
      Cryptomine", "Greenidge Power Plant and Data Center"). 07 §2.2 admits crypto sites only
      when they convert to, or are marketed as, data centers. A data center building on a site
      that also holds a mine (the Susquehanna campus) keeps the cluster in scope;
    - a room or a non-compute use (every member with text): a computer or server room, a lab or
      classroom, a node "somewhere in this building", a records or tape vault ("SDSU Computer
      Room", "Vital Records");
    - an outbuilding: every member a shed, hut, cabin, kiosk or container, or tagged
      utility=telecom (a 5 m x 5 m telecom shed on a school campus);

    The rest apply only when no member's text says data center, colocation, hosting, cloud,
    servers or computing:

    - under 200 m² of bounding boxes in all: a hut, or a suite drawn as its own polygon;
    - a point with a business name: every member a node, none with an operator, and a name that
      does not start with an operator known elsewhere in the input ("K-Motion Interactive",
      "Accelera Data Systems", "IT");
    - a telephone company's building or a government office by name: every named member is
      called only by a telephone company brand ("CenturyLink", "Verizon"), or every member with a
      name or operator names a city, county, town or village ("City of Searcy").
    """
    members = cluster.members
    mines = {m.ref: why for m in members if (why := _mine(m)) is not None}
    told = [m for m in members if _texts(m, (*_TEXT_KEYS, "operator"))]
    areas = [m for m in members if m.kind in AREA_KINDS and m.ref in mines]
    if areas or (told and all(m.ref in mines for m in told)):
        m = (areas or told)[0]
        return f"{m.ref} {mines[m.ref]}"
    texts = [(m, _texts(m)) for m in members]
    rooms = [_ROOM_RE.search(t) for _, t in texts if t]
    if rooms and all(found is not None for found in rooms):
        m = next(m for m, t in texts if t)
        words = rooms[0].group(0) if rooms[0] is not None else ""
        return f"{m.ref} is named or described as a room or a non-compute use ({words!r})"
    shaped = all(m.bounds is not None for m in members)
    if shaped and all(
        m.tags.get("building") in _OUTBUILDINGS or m.tags.get("utility") == "telecom"
        for m in members
    ):
        tag = f"building={b}" if (b := members[0].tags.get("building")) in _OUTBUILDINGS else ""
        return f"{members[0].ref} is an outbuilding ({tag or 'utility=telecom'})"
    if any(_DATA_CENTER_RE.search(_texts(m, (*_TEXT_KEYS, "operator"))) for m in members):
        return None
    if shaped and sum(m.area_m2() for m in members) < _MIN_FOOTPRINT_M2:
        return f"{members[0].ref} covers under {_MIN_FOOTPRINT_M2:g} m², a hut or a room"
    names = [m.name for m in members if m.name]
    if (
        names
        and all(m.kind == "point" and not m.has_operator for m in members)
        and not any(_known_operator(n, operators) for n in names)
    ):
        return f"{members[0].ref} is a point with a business name and no operator ({names[0]!r})"
    if names and all(normalize_name(n) in _TELEPHONE_BRANDS for n in names):
        return f"{members[0].ref} is named only as a telephone company ({names[0]!r})"
    named = [m for m in members if m.operator or m.name]
    if named and all(
        any(v and _GOVERNMENT_RE.match(v) for v in (m.operator, m.name)) for m in named
    ):
        value = next(v for v in (named[0].operator, named[0].name) if v and _GOVERNMENT_RE.match(v))
        return f"{named[0].ref} is a local government's building ({value!r})"
    return None


def _mine(m: OsmObject) -> str | None:
    """Why the member is a cryptocurrency mine or a power plant, or None."""
    crypto = _CRYPTO_RE.search(_texts(m, (*_TEXT_KEYS, "operator")))
    if crypto is not None:
        return f"is named as a cryptocurrency mine ({crypto.group(0)!r})"
    tags = m.tags
    if tags.get("industrial") in _CRYPTO_INDUSTRIAL or any(
        tags.get(k) in _CRYPTO_VALUES for k in ("data_centre", "data_center")
    ):
        return "is tagged as a cryptocurrency mine"
    if tags.get("power") == "plant" or "generator:source" in tags:
        return "is tagged as a power plant"
    return None


def _cluster_point(cluster: Cluster) -> tuple[float, float]:
    rep = cluster.representative
    if rep.kind in AREA_KINDS:
        lat, lon = rep.lat, rep.lon
    else:
        lat = sum(m.lat for m in cluster.members) / len(cluster.members)
        lon = sum(m.lon for m in cluster.members) / len(cluster.members)
    return round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS)


def _rep_point(cluster: Cluster) -> tuple[float, float]:
    rep = cluster.representative
    return round(rep.lat, COORD_DECIMALS), round(rep.lon, COORD_DECIMALS)


def _pnnl_keys(cluster: Cluster, ctx: _Context) -> list[JsonValue]:
    join = ctx.pnnl_join
    rows = join.matched.get(cluster.representative.ref, []) if join is not None else []
    return [r.key for r in rows]


def _hold(cluster: Cluster, ctx: _Context) -> ReviewItem | None:
    """A review item instead of a record for a cluster OSM gives too little to publish (07 §2.3:
    no status or date is invented), or None:

    - a site polygon (industrial=data_centre) with no data center object inside: OSM says a data
      center site is there, not whether it is built (Microsoft Hoffman Estates is tagged
      landuse=industrial before construction);
    - a cluster announced only by OSM lifecycle tags, with no name and no operator: a lead, like
      an unverified AI GridWatch row;
    - an unnamed building without an operator drawn over another building's footprint (its box
      covers most of the other's): the same building mapped twice (Apple Mesa, way 567575425
      on way 300974499).
    """
    rep = cluster.representative
    data: dict[str, JsonValue] = {"refs": list(cluster.refs), "pnnl": _pnnl_keys(cluster, ctx)}
    groups = _status_groups(cluster.members)
    if not groups:
        if any(m.kind == "site" for m in cluster.members):
            reason = (
                "an OpenStreetMap site polygon tagged industrial=data_centre with no data center "
                "building inside; OpenStreetMap does not say whether it is built"
            )
        else:
            reason = "no member carries a data center tag the crosswalk knows"
        return _review(
            "unknown_status", reason, external_id=rep.ref, data=data | {"name": rep.name}
        )
    if [g.status.status for g in groups] == ["announced"] and not any(
        m.name or m.has_operator for m in cluster.members
    ):
        label = groups[0].status.label
        return _review(
            "unverified_upstream",
            f"planned in OpenStreetMap only ({label}), with no name and no operator; a lead "
            "until a citable source names the project",
            external_id=rep.ref,
            data=data,
        )
    return None


def _duplicate_footprints(clusters: Sequence[Cluster]) -> dict[str, OsmObject]:
    """Single unnamed, operator-less buildings whose box overlaps at least DUPLICATE_OVERLAP of
    another cluster's building box (by the smaller box): ref -> the other building."""
    buildings = [m for c in clusters for m in c.members if m.kind == "building" and m.bounds]
    cluster_of = {m.ref: i for i, c in enumerate(clusters) for m in c.members}
    found: dict[str, OsmObject] = {}
    for c in clusters:
        (m, *rest) = c.members
        if rest or m.kind != "building" or m.bounds is None or m.name or m.has_operator:
            continue
        for other in buildings:
            if cluster_of[other.ref] == cluster_of[m.ref] or other.bounds is None:
                continue
            if _overlap(m.bounds, other.bounds) >= DUPLICATE_OVERLAP:
                found[m.ref] = other
                break
    return found


def _overlap(a: Bounds, b: Bounds) -> float:
    """The share of the smaller box that the two boxes have in common."""
    dlat = min(a.maxlat, b.maxlat) - max(a.minlat, b.minlat)
    dlon = min(a.maxlon, b.maxlon) - max(a.minlon, b.minlon)
    if dlat <= 0 or dlon <= 0:
        return 0.0
    smaller = min(
        (a.maxlat - a.minlat) * (a.maxlon - a.minlon), (b.maxlat - b.minlat) * (b.maxlon - b.minlon)
    )
    return dlat * dlon / smaller if smaller > 0 else 0.0


def _build_one(
    cluster: Cluster,
    point: tuple[float, float],
    county: County | None,
    ctx: _Context,
    review: list[ReviewItem],
    city: str | None = None,
) -> Candidate | None:
    rep = cluster.representative
    ordered = [rep] + [m for m in cluster.members if m.ref != rep.ref]
    existing_id = _existing_id(cluster, ctx)
    if county is None:
        review.append(
            _review(
                "missing_location",
                f"no Census county contains ({point[0]}, {point[1]})",
                external_id=rep.ref,
                data={"refs": list(cluster.refs)},
            )
        )
        return None
    groups = _status_groups(ordered)

    st = county.state_abbr
    telecom = telecom_site(cluster)
    doubt = None if telecom is not None else scope_doubt(cluster, ctx.operators)
    scope: Literal["in_scope", "out_of_scope"] = (
        "in_scope" if st in IN_SCOPE and telecom is None and doubt is None else "out_of_scope"
    )
    approximate = _approximate(cluster)
    if approximate is not None:
        m, text = approximate
        review.append(
            _review(
                "unverified_upstream",
                f"OpenStreetMap says the position of {m.ref} is approximate ({text[:120]!r}); "
                "location precision is site, not footprint",
                external_id=rep.ref,
                data={"osm": m.ref},
            )
        )
    location = _location(cluster, ordered, county, *point, approximate is not None, city)
    operator = _most_common(m.operator for m in ordered)
    owner = _most_common(m.tags.get("owner") for m in ordered)
    place = city or ctx.gazetteer.county_full_name(county.fips) or county.name
    name, base_name = _canonical_name(rep, ordered, operator, place, st)

    aliases: list[Alias] = []
    seen_names = {normalize_name(base_name)} if base_name else set()
    for m in ordered:
        key = normalize_name(m.name) if m.name else ""
        if key and key not in seen_names:
            seen_names.add(key)
            aliases.append(Alias(name=m.name or "", kind="osm_name", source_ids=[OSM_SOURCE_ID]))

    capacity, capacity_pointers = _capacity(cluster, ordered, review)
    existing = ctx.existing.get(existing_id) if existing_id else None
    events, phases, phase_of = _events(groups, cluster, ctx, review)
    events = _keep_first_reported(events, existing)

    join = ctx.pnnl_join
    pnnl_rows = join.matched.get(rep.ref, []) if join is not None else []
    site = pnnl.site_values(cluster, pnnl_rows, join.members if join is not None else {})
    review.extend(site.review)
    buildings = [
        Building(ref=f"osm:{m.ref}", name=m.name, phase_id=phase_of.get(m.ref))
        for m in cluster.members
        if m.kind not in AREA_KINDS
    ]

    sources = [
        Source(
            id=OSM_SOURCE_ID,
            url=HttpUrl(f"https://www.openstreetmap.org/{rep.ref}"),
            publisher=OSM_PUBLISHER,
            title=f"OpenStreetMap {rep.ref}",
            source_type="open_dataset",
            license=OSM_LICENSE,
            retrieved_at=ctx.osm_retrieved_at,
            supports=[*OSM_SUPPORTS, "/purpose"] if telecom is not None else list(OSM_SUPPORTS),
        )
    ]
    external_ids: dict[str, list[str]] = {"osm": sorted(cluster.refs, key=ref_key)}
    if pnnl_rows and ctx.pnnl_snapshot is not None:
        sources.append(pnnl.pnnl_source(ctx.pnnl_snapshot, PNNL_SOURCE_ID))
        external_ids["pnnl_im3"] = sorted({r.key for r in pnnl_rows})
        mismatches = pnnl.county_mismatches(
            pnnl_rows, county_fips=county.fips, state_abbr=st, counties=ctx.counties
        )
        for row, reason in mismatches:
            review.append(
                _review(
                    "county_mismatch",
                    reason,
                    external_id=row.key,
                    data={"osm": rep.ref, "county_fips": county.fips, "pnnl": row.to_json()},
                    source=pnnl.REVIEW_SOURCE,
                )
            )

    field_meta = {
        "/location/county_fips": FieldMeta(confidence=COUNTY_CONFIDENCE, method="derived"),
        "/status": FieldMeta(
            confidence=STATUS_CONFIDENCE, method="imported", source_ids=[OSM_SOURCE_ID]
        ),
    }
    for pointer in capacity_pointers:
        field_meta[pointer] = FieldMeta(
            confidence=CAPACITY_CONFIDENCE, method="imported", source_ids=[OSM_SOURCE_ID]
        )

    try:
        record = FacilityRecord(
            id=PLACEHOLDER_ID,
            record_type="campus",
            scope=scope,
            canonical_name=name,
            aliases=aliases,
            parties=Parties(
                operator=[OrgRef(name=operator, source_ids=[OSM_SOURCE_ID])] if operator else [],
                owner=[OrgRef(name=owner, source_ids=[OSM_SOURCE_ID])] if owner else [],
            ),
            purpose="telecom" if telecom is not None else "unknown",
            status=groups[0].status.status,
            evidence_level="reported",
            status_history=events,
            location=location,
            capacity=capacity,
            phases=phases,
            buildings=buildings,
            site=Site(acreage=site.acreage),
            external_ids=external_ids,
            sources=sources,
            field_meta=field_meta,
            review=Review(state="machine"),
            created_at=ctx.now,
            updated_at=ctx.now,
            last_verified_at=ctx.now,
        )
    except ValidationError as e:
        review.append(
            _review(
                "invalid",
                "the cluster does not fit the record schema",
                external_id=rep.ref,
                data={"error": str(e)[:2000]},
            )
        )
        return None
    if scope == "out_of_scope":
        if st not in IN_SCOPE:
            why = f"{st} is outside the 50 states and DC; kept with scope out_of_scope (07 §2.2)"
        elif telecom is not None:
            why = (
                f"a telecom site, not a data center: {telecom}; kept with scope out_of_scope "
                "(07 §2.2)"
            )
        else:
            why = (
                f"possibly not a data center (07 §2.2): {doubt}; held with scope out_of_scope "
                "until a reviewer decides"
            )
        review.append(_review("out_of_scope", why, external_id=rep.ref))
    return Candidate(match_values=tuple(external_ids["osm"]), record=apply_rollup(record))


def _extent(cluster: Cluster) -> Bounds:
    boxes = [m.extent for m in cluster.members]
    return Bounds(
        min(b.minlat for b in boxes),
        min(b.minlon for b in boxes),
        max(b.maxlat for b in boxes),
        max(b.maxlon for b in boxes),
    )


def _shares_number(inner: OsmObject, outer: OsmObject) -> bool:
    """True when inner's addr:housenumber is one of outer's (a list separated by ";")."""
    number = inner.tags.get("addr:housenumber", "").strip()
    numbers = {n.strip() for n in outer.tags.get("addr:housenumber", "").split(";")}
    return bool(number) and number in numbers


def _chains(extents: Sequence[Bounds], limit_m: float) -> list[list[int]]:
    """Indexes of extents grouped by chains of gaps within limit_m, in index order."""
    group = list(range(len(extents)))
    for i in range(len(extents)):
        for j in range(i + 1, len(extents)):
            if group[j] != group[i] and extents[i].gap_m(extents[j]) <= limit_m:
                old, new = max(group[i], group[j]), min(group[i], group[j])
                group = [new if g == old else g for g in group]
    out: dict[int, list[int]] = {}
    for i, g in enumerate(group):
        out.setdefault(g, []).append(i)
    return list(out.values())


def _possible_duplicates(
    built: Sequence[tuple[Cluster, Candidate]], objects: Sequence[OsmObject]
) -> list[ReviewItem]:
    """possible_duplicate items for records that may describe one site: in-scope records with the
    same canonical name, chained within DUPLICATE_NAME_GAP_M of each other (one item per group);
    a node inside a building whose operator disagrees; and an object inside a campus or site whose
    operator disagrees, when that campus has no building of its own or lists the object's house
    number (the operator guard kept them apart)."""
    items: list[ReviewItem] = []
    by_name: dict[str, list[Cluster]] = {}
    for cluster, cand in built:
        if cand.record.scope == "in_scope":
            by_name.setdefault(cand.record.canonical_name, []).append(cluster)
    for name, same in sorted(by_name.items()):
        for group in _chains([_extent(c) for c in same], DUPLICATE_NAME_GAP_M):
            if len(group) < 2:
                continue
            reps = [same[i].representative.ref for i in group]
            items.append(
                _review(
                    "possible_duplicate",
                    f"{len(group)} records are called {name!r}, each within "
                    f"{DUPLICATE_NAME_GAP_M:,.0f} m of another: they may be one site that the "
                    "dissolve rules split",
                    external_id=reps[0],
                    data={"representatives": list(reps)},
                )
            )
    rep_of = {m.ref: c.representative.ref for c, _ in built for m in c.members}
    alone = {c.representative.ref for c, _ in built if all(m.kind in AREA_KINDS for m in c.members)}
    pairs: dict[tuple[str, str], list[tuple[OsmObject, OsmObject]]] = {}
    for inner, outer in operator_conflicts(objects):
        if inner.ref not in rep_of or outer.ref not in rep_of:
            continue
        if rep_of[inner.ref] == rep_of[outer.ref]:
            continue
        # A campus box covers other operators' buildings in dense areas (Equinix DC17 in an AWS
        # box): that is a doubt only when the campus has no building of its own, or lists the
        # building's house number (Microsoft's Quail Ridge Lane polygon over AWS IAD-124..127).
        if outer.kind in AREA_KINDS and not (
            rep_of[outer.ref] in alone or _shares_number(inner, outer)
        ):
            continue
        pairs.setdefault((rep_of[inner.ref], rep_of[outer.ref]), []).append((inner, outer))
    for (inner_rep, outer_rep), found in sorted(pairs.items()):
        inner, outer = found[0]
        inside = [i.ref for i, _ in found]
        items.append(
            _review(
                "possible_duplicate",
                f"{', '.join(inside)} (operator {inner.operator or inner.operator_qid!r}) "
                f"{'lies' if len(inside) == 1 else 'lie'} inside {outer.ref} (operator "
                f"{outer.operator or outer.operator_qid!r}); the "
                "operators disagree, so they were not joined, but one record may describe the "
                "other's site",
                external_id=inner_rep,
                data={"osm": list(inside), "other": outer.ref, "other_record": outer_rep},
            )
        )
    return items


def build_candidates(
    clusters: Sequence[Cluster],
    *,
    counties: CountyIndex,
    now: datetime,
    snapshot: InputSnapshot,
    pnnl_join: pnnl.JoinResult | None = None,
    pnnl_snapshot: InputSnapshot | None = None,
    existing: Mapping[str, FacilityRecord] | None = None,
    gazetteer: Gazetteer | None = None,
    places: PlaceIndex | None = None,
) -> BuildResult:
    """One campus candidate per cluster that is not held for review, the review items, and
    metrics. places (the Census place polygons) gives the city: the place that contains the
    record's point, more than about 100 m inside its line; without one, or without places, the
    record names no city and its canonical name gives the county's full name, from gazetteer
    (default: the committed Census Gazetteer)."""
    if snapshot.upstream_version is None:
        raise ValueError("the Overpass snapshot has no timestamp_osm_base")
    if gazetteer is None:
        from atlas.geocode import Gazetteer  # the Census files load on use

        gazetteer = Gazetteer.load()
    as_of_date = date.fromisoformat(snapshot.upstream_version[:10])
    stored = dict(existing or {})
    by_ref: dict[str, set[str]] = {}
    for rid, record in stored.items():
        for ref in record.external_ids.get("osm", []):
            by_ref.setdefault(ref, set()).add(rid)
    objects = [m for c in clusters for m in c.members]
    operators = frozenset(
        key
        for m in objects
        for value in (m.operator, m.tags.get("operator:short"))
        if value and (key := normalize_org(value))
    )
    ctx = _Context(
        counties=counties,
        gazetteer=gazetteer,
        now=now,
        as_of=FuzzyDate(value=as_of_date.isoformat(), precision="day"),
        as_of_date=as_of_date,
        latest_date=add_years(as_of_date, FUTURE_YEARS),
        osm_retrieved_at=snapshot.retrieved_at,
        pnnl_join=pnnl_join,
        pnnl_snapshot=pnnl_snapshot,
        existing=stored,
        existing_by_ref=by_ref,
        operators=operators,
    )
    # The record's county is the representative's: a mean point can fall in the next county.
    points = [_cluster_point(c) for c in clusters]
    reps = [_rep_point(c) for c in clusters]
    at_point = counties.lookup_many(points)
    at_rep = counties.lookup_many(reps)
    review: list[ReviewItem] = []
    candidates: list[Candidate] = []
    built: list[tuple[Cluster, Candidate]] = []
    duplicates = _duplicate_footprints(clusters)
    held: set[str] = set()
    chosen: list[tuple[Cluster, tuple[float, float], County | None]] = []
    for cluster, point, rep_point, mean_county, rep_county in zip(
        clusters, points, reps, at_point, at_rep, strict=True
    ):
        item = _hold(cluster, ctx)
        twin = duplicates.get(cluster.representative.ref)
        if item is None and twin is not None:
            item = _review(
                "possible_duplicate",
                f"an unnamed building without an operator drawn over {twin.ref} "
                f"({twin.name or 'unnamed'}): most likely the same building mapped twice; no "
                "record is made",
                external_id=cluster.representative.ref,
                data={"refs": list(cluster.refs), "other": twin.ref},
            )
        if item is not None:
            review.append(item)
            held.add(cluster.representative.ref)
            continue
        county = rep_county or mean_county
        if county is not None and (mean_county is None or mean_county.fips != county.fips):
            point = rep_point
        chosen.append((cluster, point, county))
    found = places.containing_many([p for _, p, _ in chosen]) if places is not None else []
    for (cluster, point, county), place_found in zip(
        chosen, found or [None] * len(chosen), strict=True
    ):
        city = (
            place_found.base_name
            if place_found is not None
            and county is not None
            and place_found.state_abbr == county.state_abbr
            else None
        )
        candidate = _build_one(cluster, point, county, ctx, review, city)
        if candidate is not None:
            candidates.append(candidate)
            built.append((cluster, candidate))
    review.extend(_possible_duplicates(built, objects))
    metrics: dict[str, float | int] = {
        "objects": len(objects),
        "clusters": len(clusters),
        "held": len(held),
        "out_of_scope": sum(1 for c in candidates if c.record.scope == "out_of_scope"),
        "unit_parse": sum(1 for r in review if r.kind == "unit_parse"),
    }
    if pnnl_join is not None:
        for row in pnnl_join.unmatched:
            review.append(pnnl.unmatched_item(row, pnnl_join.nearest.get(row.key)))
        metrics["pnnl_rows"] = pnnl_join.total
        metrics["pnnl_matched"] = pnnl_join.total - len(pnnl_join.unmatched)
        metrics["pnnl_matched_by_id"] = pnnl_join.by_id
        metrics["pnnl_matched_by_name"] = pnnl_join.by_name
        metrics["pnnl_on_held"] = sum(len(pnnl_join.matched.get(ref, [])) for ref in held)
        metrics["pnnl_match_rate"] = round(pnnl_join.match_rate, 4)
    return BuildResult(candidates=candidates, review=review, metrics=metrics)


# ---------------------------------------------------------------------------- importer


def _positive_m(value: str) -> float:
    try:
        meters = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from e
    if not 0 < meters <= 5_000:
        raise argparse.ArgumentTypeError(f"must be in (0, 5000] metres, not {value}")
    return meters


class OsmImporter:
    """`atlas import osm`."""

    name = "osm"
    match_key = "osm"
    owned_external_keys = ("osm", "pnnl_im3")
    review_sources = ("osm", "pnnl")
    version = "2"
    help = (
        "OpenStreetMap data centers via Overpass, dissolved into campuses, checked against PNNL IM3"
    )

    def __init__(
        self,
        *,
        min_elements: int = MIN_ELEMENTS,
        retries: int = OVERPASS_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_elements = min_elements
        self.retries = retries
        self.sleep = sleep

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--pnnl",
            type=Path,
            default=None,
            metavar="PATH",
            help="PNNL GeoJSON or MSD-LIVE CSV (default: fetch the public GeoJSON)",
        )
        group.add_argument("--no-pnnl", action="store_true", help="skip the PNNL cross-check")
        parser.add_argument(
            "--overpass-url",
            action="append",
            default=None,
            metavar="URL",
            help="Overpass endpoint, repeatable; replaces the default list",
        )
        parser.add_argument(
            "--dissolve-m",
            type=_positive_m,
            default=DEFAULT_RADIUS_M,
            metavar="M",
            help="same-operator join distance between bounding boxes in metres (default 300)",
        )

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        endpoints: list[str] = list(getattr(args, "overpass_url", None) or OVERPASS_ENDPOINTS)
        overpass = fetch_overpass(
            ctx, endpoints, min_elements=self.min_elements, retries=self.retries, sleep=self.sleep
        )
        objects, skipped = parse_overpass(overpass.doc)
        counties = ctx.counties()
        found = counties.lookup_many([(o.lat, o.lon) for o in objects])
        regions = {
            o.ref: c.fips if c is not None else None for o, c in zip(objects, found, strict=True)
        }
        clusters = dissolve(
            objects,
            radius_m=float(getattr(args, "dissolve_m", DEFAULT_RADIUS_M)),
            regions=regions,
        )
        inputs = [overpass.snapshot]
        join: pnnl.JoinResult | None = None
        pnnl_snapshot: InputSnapshot | None = None
        if not getattr(args, "no_pnnl", False):
            rows, pnnl_snapshot = pnnl.load_pnnl_input(
                ctx, getattr(args, "pnnl", None), sleep=self.sleep
            )
            inputs.append(pnnl_snapshot)
            join = pnnl.join(clusters, rows)
        built = build_candidates(
            clusters,
            counties=counties,
            now=ctx.now,
            snapshot=overpass.snapshot,
            pnnl_join=join,
            pnnl_snapshot=pnnl_snapshot,
            existing=ctx.records,
            places=ctx.places(),
        )
        for ref in skipped:
            built.review.append(
                _review("missing_location", "the Overpass element has no position", external_id=ref)
            )
        built.metrics["skipped"] = len(skipped)
        if join is not None and join.match_rate < pnnl.MATCH_RATE_TARGET:
            print(
                f"warning: {join.match_rate:.1%} of PNNL rows matched an OSM-derived record, below "
                f"the {pnnl.MATCH_RATE_TARGET:.0%} M1 acceptance threshold (07 §15)",
                file=sys.stderr,
            )
        return ImportResult(
            source=self.name,
            inputs=inputs,
            candidates=built.candidates,
            review=built.review,
            metrics=built.metrics,
        )


IMPORTER = OsmImporter()
