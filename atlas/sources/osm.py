"""OpenStreetMap seed importer: data centers from Overpass, dissolved into campuses and cross-checked
with PNNL IM3 (07 §4.1, §4.2, §5.1, §6.5 step 1). docs/sources/osm-pnnl.md describes the mapping.

1. Fetch: one Overpass query (OVERPASS_QUERY) POSTed to each endpoint in turn until one answers
   with a complete result. Overpass can answer HTTP 200 with a partial result, so a `remark` with
   "runtime error", a missing osm3s.timestamp_osm_base or fewer than MIN_ELEMENTS elements also
   counts as a failure. `--input FILE` reads a saved response instead (no element minimum).
2. Parse every element into an OsmObject (center = node position or bounding-box midpoint).
3. Dissolve objects into campuses (atlas.dissolve).
4. Join PNNL rows to the clusters (atlas.sources.pnnl) unless --no-pnnl.
5. Build one campus record per cluster, with external_ids["osm"] = every member ref.

Data is © OpenStreetMap contributors, ODbL 1.0; PNNL IM3 is ODbL 1.0 (07 §5.1).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import HttpUrl, JsonValue, ValidationError

from atlas.crosswalk import Crosswalked, from_osm_tags
from atlas.dissolve import (
    DEFAULT_RADIUS_M,
    Bounds,
    Cluster,
    ObjectKind,
    OsmObject,
    dissolve,
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
from atlas.text import normalize_name, strip_invisible
from atlas.validate import EARLIEST_DATE, FUTURE_YEARS, add_years

if TYPE_CHECKING:
    from atlas.geo.counties import County, CountyIndex
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
    "parse_mw",
    "parse_overpass",
    "telecom_site",
]

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
# `out tags bb` (not the plan's `out center tags`): campus containment needs the extents, and the
# center is the bounding-box midpoint, which is what `center` returns.
OVERPASS_QUERY = """[out:json][timeout:180];
area["ISO3166-1"="US"][admin_level=2]->.us;
(
  nwr["telecom"="data_center"](area.us);
  nwr["building"="data_center"](area.us);
  nwr["construction:telecom"="data_center"](area.us);
  nwr["proposed:telecom"="data_center"](area.us);
  nwr["construction"="data_center"](area.us);
);
out tags bb;"""
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
    "proposed": "Proposed",
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


class OverpassError(ValueError):
    """An Overpass response that is not a complete result."""


# ---------------------------------------------------------------------------- parse


def _clean(value: object) -> str:
    return " ".join(strip_invisible(str(value)).split())


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


def classify(osm_type: str, tags: Mapping[str, str]) -> ObjectKind:
    """campus: a way or relation with telecom=data_center and no building tag (a lifecycle one,
    proposed:building=* or construction:building=*, counts as a building tag), or building=no;
    building: any other way or relation; point: a node.

    A planned building drawn as its own polygon is one building of a site, not a campus: as a
    campus, each of Rowan Green's four proposed:building=industrial halls was a record of its own,
    and QTS Hillsboro 3's two polygons split the site (2026-10-07).
    """
    if osm_type == "node":
        return "point"
    if tags.get("building") == "no":
        return "campus"
    is_building = "building" in tags or any(
        tags.get(key, "no") != "no" for key in LIFECYCLE_BUILDING_KEYS
    )
    if tags.get("telecom") == "data_center" and not is_building:
        return "campus"
    return "building"


def parse_overpass(doc: Mapping[str, Any]) -> tuple[list[OsmObject], list[str]]:
    """OsmObjects from an Overpass `out tags bb` (or `out center`) result, and the refs of
    elements that had no position."""
    objects: list[OsmObject] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for element in doc.get("elements", []):
        if not isinstance(element, dict):
            continue
        osm_type, osm_id = element.get("type"), element.get("id")
        if osm_type not in ("node", "way", "relation") or not isinstance(osm_id, int):
            continue
        ref = f"{osm_type}/{osm_id}"
        if ref in seen:
            continue
        seen.add(ref)
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
            skipped.append(ref)
            continue
        objects.append(OsmObject(ref, lat, lon, bounds, tags, classify(osm_type, tags)))
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


def _street(m: OsmObject) -> str | None:
    street = m.tags.get("addr:street")
    if not street:
        return None
    number = m.tags.get("addr:housenumber")
    return f"{number} {street}" if number else street


def _pick(
    rep: OsmObject, ordered: Sequence[OsmObject], get: Callable[[OsmObject], str | None]
) -> str | None:
    """The representative's value, else the most common member value."""
    return get(rep) or _most_common(get(m) for m in ordered)


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
    """Members grouped by crosswalked status, most advanced first (operating, under_construction,
    proposed). Members are kept in `ordered` order, so the representative leads its group."""
    by_status: dict[str, list[tuple[Crosswalked, OsmObject]]] = {}
    for m in ordered:
        found = from_osm_tags(m.tags)
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
        buildings = [m for m in cluster.members if m.kind != "campus"]
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


def _phase_name(group: _Group) -> str:
    names = list(dict.fromkeys(m.name or m.ref for m in group.members))
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return f"{_PHASE_LABEL[group.status.status]} in OpenStreetMap: {shown}"


def _events(
    groups: Sequence[_Group], rep: OsmObject, ctx: _Context
) -> tuple[list[StatusEvent], list[Phase], dict[str, str]]:
    """Status events, phases and each member's phase_id.

    One event per status group, from the crosswalk, at the snapshot date; an operating group
    whose representative (else earliest member) has a start_date gets an energized event at that
    date instead. When members disagree, each group becomes a phase ("osm-{status}"), so an
    operating campus with a building under construction stays operating and is expanding (07
    §2.3). A non-operating group with an opening_date adds a planned energized event.
    """
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
        start = _start_date(group, rep, ctx) if status.status == "operating" else None
        if start is not None:
            as_of, ref = start
            event = StatusEvent(
                seq=len(events) + 1,
                status="operating",
                event="energized",
                as_of=as_of,
                phase_id=phase_id,
                source_ids=[OSM_SOURCE_ID],
                note=f"start_date={as_of.value} on {ref}; tagged {status.label} in OpenStreetMap",
            )
        else:
            event = StatusEvent(
                seq=len(events) + 1,
                status=status.status,
                event=status.event,
                as_of=ctx.as_of,
                phase_id=phase_id,
                source_ids=[OSM_SOURCE_ID],
                note=f"Tagged {status.label} in OpenStreetMap; status per 07 §4.7",
            )
        events.append(event)
    for group, phase_id in zip(groups, phase_ids, strict=True):
        if group.status.status == "operating":
            continue
        raw = _pick(group.members[0], group.members, lambda m: m.tags.get("opening_date"))
        opening = osm_date(raw, latest=ctx.latest_date)
        if opening is not None:
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


def _keep_first_reported(
    events: list[StatusEvent], existing: FacilityRecord | None
) -> list[StatusEvent]:
    """Keep the earliest stored as_of of a first_reported event with the same status and sources,
    so weekly runs never move it forward.

    The phase is not compared: when a cluster gains or loses a status group, its events move
    between phase_id None and "osm-{status}", but the status itself has not changed.
    """
    if existing is None:
        return events
    stored: dict[tuple[str, tuple[str, ...]], FuzzyDate] = {}
    for e in existing.status_history:
        if e.planned or e.event != "first_reported":
            continue
        key = (e.status, tuple(e.source_ids))
        if key not in stored or period_start(e.as_of) < period_start(stored[key]):
            stored[key] = e.as_of
    out: list[StatusEvent] = []
    for e in events:
        old = stored.get((e.status, tuple(e.source_ids)))
        if (
            e.event == "first_reported"
            and not e.planned
            and old is not None
            and period_start(old) <= period_start(e.as_of)
        ):
            e = e.model_copy(update={"as_of": old.model_copy()})
        out.append(e)
    return out


def _location(
    cluster: Cluster, ordered: Sequence[OsmObject], county: County, lat: float, lon: float
) -> Location:
    rep = cluster.representative
    shaped = any(m.osm_type in ("way", "relation") for m in cluster.members)
    return Location(
        lat=lat,
        lon=lon,
        precision="footprint" if shaped else "site",
        geometry_ref=f"osm:{rep.ref}",
        street=_pick(rep, ordered, _street),
        city=_pick(rep, ordered, lambda m: m.tags.get("addr:city")),
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
    rep: OsmObject, ordered: Sequence[OsmObject], operator: str | None, place: str | None, st: str
) -> tuple[str, str | None]:
    """(canonical_name, the member name used as its base)."""
    base_name = rep.name or _most_common(m.name for m in ordered)
    base = base_name or (f"{operator} data center" if operator else "Data center")
    if operator and not _starts_with_operator(base, operator, rep.tags.get("operator:short")):
        base = f"{operator} {base}"
    return (f"{base} ({place}, {st})" if place else f"{base} ({st})"), base_name


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


def _cluster_point(cluster: Cluster) -> tuple[float, float]:
    rep = cluster.representative
    if rep.kind == "campus":
        lat, lon = rep.lat, rep.lon
    else:
        lat = sum(m.lat for m in cluster.members) / len(cluster.members)
        lon = sum(m.lon for m in cluster.members) / len(cluster.members)
    return round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS)


def _build_one(
    cluster: Cluster,
    point: tuple[float, float],
    county: County | None,
    ctx: _Context,
    review: list[ReviewItem],
) -> Candidate | None:
    rep = cluster.representative
    ordered = [rep] + [m for m in cluster.members if m.ref != rep.ref]
    existing_id = _existing_id(cluster, ctx)
    if county is None:
        county = ctx.counties.lookup(rep.lat, rep.lon)
        if county is not None:
            point = (round(rep.lat, COORD_DECIMALS), round(rep.lon, COORD_DECIMALS))
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
    if not groups:
        review.append(
            _review(
                "unknown_status",
                "no member carries a data center tag the crosswalk knows",
                external_id=rep.ref,
            )
        )
        return None

    st = county.state_abbr
    telecom = telecom_site(cluster)
    scope: Literal["in_scope", "out_of_scope"] = (
        "in_scope" if st in IN_SCOPE and telecom is None else "out_of_scope"
    )
    location = _location(cluster, ordered, county, *point)
    operator = _most_common(m.operator for m in ordered)
    owner = _most_common(m.tags.get("owner") for m in ordered)
    place = location.city or ctx.gazetteer.county_full_name(county.fips) or county.name
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
    events, phases, phase_of = _events(groups, rep, ctx)
    events = _keep_first_reported(events, existing)

    join = ctx.pnnl_join
    pnnl_rows = join.matched.get(rep.ref, []) if join is not None else []
    site = pnnl.site_values(cluster, pnnl_rows, join.members if join is not None else {})
    review.extend(site.review)
    buildings = [
        Building(
            ref=f"osm:{m.ref}",
            name=m.name,
            sqft=site.building_sqft.get(m.ref),
            phase_id=phase_of.get(m.ref),
        )
        for m in cluster.members
        if m.kind != "campus"
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
            site=Site(building_sqft=site.site_sqft, acreage=site.acreage),
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
        why = (
            f"{st} is outside the 50 states and DC"
            if st not in IN_SCOPE
            else f"a telecom site, not a data center: {telecom}"
        )
        review.append(
            _review(
                "out_of_scope",
                f"{why}; kept with scope out_of_scope (07 §2.2)",
                external_id=rep.ref,
            )
        )
    return Candidate(match_values=tuple(external_ids["osm"]), record=apply_rollup(record))


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
) -> BuildResult:
    """One campus candidate per cluster, the review items, and metrics. gazetteer (default: the
    committed Census Gazetteer) gives the county's full name for clusters without a city."""
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
    )
    points = [_cluster_point(c) for c in clusters]
    found = counties.lookup_many(points)
    review: list[ReviewItem] = []
    candidates: list[Candidate] = []
    for cluster, point, county in zip(clusters, points, found, strict=True):
        candidate = _build_one(cluster, point, county, ctx, review)
        if candidate is not None:
            candidates.append(candidate)
    metrics: dict[str, float | int] = {
        "objects": sum(len(c.members) for c in clusters),
        "clusters": len(clusters),
        "out_of_scope": sum(1 for c in candidates if c.record.scope == "out_of_scope"),
        "unit_parse": sum(1 for r in review if r.kind == "unit_parse"),
    }
    if pnnl_join is not None:
        for row in pnnl_join.unmatched:
            review.append(pnnl.unmatched_item(row))
        metrics["pnnl_rows"] = pnnl_join.total
        metrics["pnnl_matched"] = pnnl_join.total - len(pnnl_join.unmatched)
        metrics["pnnl_matched_by_id"] = pnnl_join.by_id
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
    version = "1"
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
        clusters = dissolve(objects, radius_m=float(getattr(args, "dissolve_m", DEFAULT_RADIUS_M)))
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
            counties=ctx.counties(),
            now=ctx.now,
            snapshot=overpass.snapshot,
            pnnl_join=join,
            pnnl_snapshot=pnnl_snapshot,
            existing=ctx.records,
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
