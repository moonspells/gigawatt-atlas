"""PNNL IM3 Open Source Data Center Atlas: the cross-check for the OSM seed (07 §4.1, §4.2 step 3).

PNNL v2026.02.09 (DOI 10.57931/3017294, ODbL 1.0) is derived from OpenStreetMap. Two layouts are
read:

- the public web-map file (PNNL_GEOJSON_URL): 1,382 Point features whose properties are only
  state_abb, county, operator, name, sqft and type. It has no OSM id and no FIPS code, and it
  does not say which dataset version it is: WEBMAP_VERSIONS records the files whose version is
  established, by SHA-256. Records built from any other web-map file cite that file, not a
  version (pnnl_source). The repository that serves it is BSD 2-Clause, © 2025 Battelle
  Memorial Institute (ATTRIBUTION.md).
- a CSV in the MSD-LIVE column layout (id, state, state_abb, state_id, county, county_id, ref,
  operator, name, sqft, lat, lon, type), if the owner mirrors the MSD-LIVE v2026.02.09 files.
  Those files need an MSD-LIVE sign-in, so this module never downloads them; MSD-LIVE is used
  only for a version check through its public records API, which warns when a newer version is
  out.

join() matches rows to dissolved OSM clusters: by OSM id when a row has one, otherwise spatially,
and failing that by name nearby. The OSM importer (atlas.sources.osm) applies the matches to its
records. This module has no IMPORTER: PNNL rows only enrich and cross-check OSM records. PNNL's
sqft is the area of the OSM footprint, not a floor area, so it is used to check the match and is
not published as building_sqft.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import HttpUrl, JsonValue

from atlas.dissolve import (
    AREA_KINDS,
    Cluster,
    OsmObject,
    haversine_m,
    meters_to_degrees,
    operators_compatible,
    ref_key,
    ring_area_m2,
)
from atlas.net import FetchError, FetchResult, fetch
from atlas.schema.record import Source
from atlas.sources.base import ImportContext, InputSnapshot, ReviewItem, load_input
from atlas.text import clean_text, normalize_name, normalize_org

if TYPE_CHECKING:
    from atlas.geo.counties import CountyIndex

PNNL_GEOJSON_URL = "https://immm-sfa.github.io/datacenter-atlas/im3_datacenter_centroids.geojson"
PNNL_WEBMAP_FILE = "im3_datacenter_centroids.geojson"
MSDLIVE_RECORD = "https://data.msdlive.org/api/records/p147s-4h760"
PNNL_DOI_URL = "https://doi.org/10.57931/3017294"
PNNL_VERSION = "v2026.02.09"
PNNL_LICENSE = "ODbL-1.0"
PNNL_PUBLISHER = "Pacific Northwest National Laboratory (IM3)"
PNNL_DATASET = "IM3 Open Source Data Center Atlas"
PNNL_TITLE = f"{PNNL_DATASET} {PNNL_VERSION}"
VERSION_DOIS = {PNNL_VERSION: PNNL_DOI_URL}  # the versions this importer was checked against
# Web-map files whose dataset version is established. This one is the file that
# immm-sfa/datacenter-atlas committed on 2026-02-12 (74ab37d, "Updated existing dc db, citation,
# doi link and last update date"), the commit that set the map's own citation to v2026.02.09 and
# its "Last Updated Feb 09, 2026". It was still the served file on 2026-10-08; the 2026-03-31
# Last-Modified is a later deploy that changed only the projected layers.
WEBMAP_VERSIONS = {
    "2e7bd7e650fe86fe0d156b4e483ebd331cfa98a0468ce33b932f8c1b6c3245df": PNNL_VERSION,
}
PNNL_SUPPORTS = ("/site",)  # site.acreage; PNNL's sqft is a footprint and is not published
REVIEW_SOURCE = "pnnl"

CSV_COLUMNS = (
    "id",
    "state",
    "state_abb",
    "state_id",
    "county",
    "county_id",
    "ref",
    "operator",
    "name",
    "sqft",
    "lat",
    "lon",
    "type",
)
ROW_TYPES = ("point", "building", "campus")
CONTAIN_MARGIN_M = 30.0  # a row point inside a member's bounding box expanded by this much
NEAR_RADIUS_M = 50.0  # or within this distance of a member's center
# A row that reaches no member matches a member of the same name and a compatible operator within
# this distance: OSM node 13311012216 (Fiberhub LAS1) moved 119 m after PNNL took its position.
NAME_RADIUS_M = 250.0
SQFT_PER_ACRE = 43_560.0
SQFT_PER_M2 = 10.763_910_4
# PNNL's sqft is the footprint polygon's area, so it cannot exceed the footprint's bounding box. 5%
# covers the difference between PNNL's area projection and the box estimate (at most 1.3% seen).
FOOTPRINT_SLACK = 1.05
MATCH_RATE_TARGET = 0.95  # 07 §15 M1 acceptance
_GRID_DEG = 0.01
_ID_RE = re.compile(r"^(\d+)(?:\.0+)?$")
_FIPS_RE = re.compile(r"^\d{5}$")
# Which OSM element types a PNNL row type can come from, most likely first.
_ID_TYPES: dict[str, tuple[str, ...]] = {
    "point": ("node",),
    "building": ("way", "relation"),
    "campus": ("way", "relation"),
}


def _clean(value: object) -> str | None:
    """A trimmed string with invisible characters removed, or None for empty and null values."""
    if value is None:
        return None
    text = clean_text(str(value))
    return text or None


def _float(value: object) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    number = float(text)
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class PnnlRow:
    """One PNNL row. osm_id and county_id are set only by the MSD-LIVE CSV layout."""

    type: str  # point | building | campus
    lat: float
    lon: float
    name: str | None = None
    operator: str | None = None
    state_abb: str | None = None
    county: str | None = None
    sqft: float | None = None
    osm_id: str | None = None
    county_id: str | None = None
    ref: str | None = None

    @property
    def key(self) -> str:
        """The external_ids["pnnl_im3"] value: "{type}:{osm_id}", else "{type}@{lon},{lat}"."""
        if self.osm_id:
            return f"{self.type}:{self.osm_id}"
        return f"{self.type}@{self.lon:.6f},{self.lat:.6f}"

    def osm_refs(self) -> tuple[str, ...]:
        """The OSM refs this row's id can stand for (empty without an id)."""
        if not self.osm_id:
            return ()
        types = _ID_TYPES.get(self.type, ("node", "way", "relation"))
        return tuple(f"{t}/{self.osm_id}" for t in types)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "key": self.key,
            "type": self.type,
            "name": self.name,
            "operator": self.operator,
            "state_abb": self.state_abb,
            "county": self.county,
            "county_id": self.county_id,
            "sqft": self.sqft,
            "lat": self.lat,
            "lon": self.lon,
            "osm_id": self.osm_id,
            "ref": self.ref,
        }


def _row_type(value: object, where: str) -> str:
    text = _clean(value)
    if text is None or text.casefold() not in ROW_TYPES:
        raise ValueError(f"{where}: type {value!r} is not one of {', '.join(ROW_TYPES)}")
    return text.casefold()


def _parse_geojson(doc: object) -> list[PnnlRow]:
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
        raise ValueError("PNNL GeoJSON: not a FeatureCollection")
    features = doc.get("features")
    if not isinstance(features, list):
        raise ValueError("PNNL GeoJSON: no features array")
    rows: list[PnnlRow] = []
    for i, feature in enumerate(features):
        where = f"PNNL feature {i}"
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if (
            not isinstance(geometry, dict)
            or geometry.get("type") != "Point"
            or not isinstance(coords, list)
            or len(coords) < 2
        ):
            raise ValueError(f"{where}: geometry is not a Point")
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            raise ValueError(f"{where}: properties is not an object")
        lon, lat = float(coords[0]), float(coords[1])
        rows.append(
            PnnlRow(
                type=_row_type(props.get("type"), where),
                lat=lat,
                lon=lon,
                name=_clean(props.get("name")),
                operator=_clean(props.get("operator")),
                state_abb=_clean(props.get("state_abb")),
                county=_clean(props.get("county")),
                sqft=_float(props.get("sqft")),
            )
        )
    return rows


def _parse_csv(text: str) -> list[PnnlRow]:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in ("type", "lat", "lon") if c not in header]
    if missing:
        raise ValueError(f"PNNL CSV: missing column(s) {', '.join(missing)}")
    reader.fieldnames = header
    rows: list[PnnlRow] = []
    for line, raw in enumerate(reader, start=2):
        where = f"PNNL CSV line {line}"
        lat, lon = _float(raw.get("lat")), _float(raw.get("lon"))
        if lat is None or lon is None:
            raise ValueError(f"{where}: lat and lon are required")
        osm_id = _clean(raw.get("id"))
        if osm_id is not None:
            match = _ID_RE.match(osm_id)
            if match is None:
                raise ValueError(f"{where}: id {osm_id!r} is not an OSM id")
            osm_id = match.group(1)
        county_id = _clean(raw.get("county_id"))
        if county_id is not None and county_id.isdigit() and len(county_id) < 5:
            county_id = county_id.zfill(5)
        rows.append(
            PnnlRow(
                type=_row_type(raw.get("type"), where),
                lat=lat,
                lon=lon,
                name=_clean(raw.get("name")),
                operator=_clean(raw.get("operator")),
                state_abb=_clean(raw.get("state_abb")),
                county=_clean(raw.get("county")),
                sqft=_float(raw.get("sqft")),
                osm_id=osm_id,
                county_id=county_id if county_id and _FIPS_RE.match(county_id) else None,
                ref=_clean(raw.get("ref")),
            )
        )
    return rows


def parse_pnnl(data: bytes) -> list[PnnlRow]:
    """Rows from the public GeoJSON or from a CSV in the MSD-LIVE layout (detected by content)."""
    text = data.decode("utf-8-sig")
    rows = _parse_geojson(json.loads(text)) if text.lstrip().startswith("{") else _parse_csv(text)
    if not rows:
        raise ValueError("PNNL file has no rows")
    return rows


def load_pnnl(path: Path) -> list[PnnlRow]:
    """Read a PNNL GeoJSON or MSD-LIVE CSV file."""
    return parse_pnnl(path.read_bytes())


# ---------------------------------------------------------------------------- join


@dataclass(frozen=True)
class JoinResult:
    """matched: representative ref -> rows (in input order); members: row key -> member ref;
    nearest: unmatched row key -> (the nearest member's ref, its distance in metres)."""

    matched: dict[str, list[PnnlRow]]
    unmatched: list[PnnlRow]
    members: dict[str, str] = field(default_factory=dict)
    by_id: int = 0
    by_name: int = 0
    nearest: dict[str, tuple[str, float]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(rows) for rows in self.matched.values()) + len(self.unmatched)

    @property
    def match_rate(self) -> float:
        """Matched rows / all rows (07 §15 M1 acceptance: at least 0.95)."""
        total = self.total
        return (total - len(self.unmatched)) / total if total else 0.0


class _Grid:
    """A coarse grid over member reach areas, so each row checks only nearby members."""

    def __init__(self, members: Iterable[OsmObject]) -> None:
        self.cells: dict[tuple[int, int], list[OsmObject]] = {}
        reach = max(CONTAIN_MARGIN_M, NEAR_RADIUS_M)
        for m in members:
            if m.bounds is not None:
                box = m.bounds.expanded(reach)
                south, west, north, east = box.minlat, box.minlon, box.maxlat, box.maxlon
            else:
                dlat, dlon = meters_to_degrees(reach, m.lat)
                south, west, north, east = m.lat - dlat, m.lon - dlon, m.lat + dlat, m.lon + dlon
            for i in range(math.floor(south / _GRID_DEG), math.floor(north / _GRID_DEG) + 1):
                for j in range(math.floor(west / _GRID_DEG), math.floor(east / _GRID_DEG) + 1):
                    self.cells.setdefault((i, j), []).append(m)

    def near(self, lat: float, lon: float) -> list[OsmObject]:
        return self.cells.get((math.floor(lat / _GRID_DEG), math.floor(lon / _GRID_DEG)), [])


def _reaches(m: OsmObject, row: PnnlRow) -> bool:
    if m.bounds is not None and m.bounds.expanded(CONTAIN_MARGIN_M).contains(row.lat, row.lon):
        return True
    return haversine_m(row.lat, row.lon, m.lat, m.lon) <= NEAR_RADIUS_M


def _same_name(row: PnnlRow, m: OsmObject) -> bool:
    return bool(row.name and m.name and normalize_name(row.name) == normalize_name(m.name))


def _same_org(row: PnnlRow, m: OsmObject) -> bool:
    if not row.operator or not m.operator:
        return False
    key = normalize_org(row.operator)
    return bool(key) and key == normalize_org(m.operator)


def spatial_match(row: PnnlRow, candidates: Sequence[OsmObject]) -> OsmObject | None:
    """The member whose expanded bounding box holds the row point, or whose center is within
    50 m. A member whose own box (no margin) holds the point comes first, then one of the row's
    kind (a campus row: a campus or site; a building or point row: a building or point); then
    the same normalized name, the same normalized operator, distance and ref break ties. A row
    on the center of the current building is that building's, whatever the old name PNNL kept
    ("QTS Manassas DC1" on DC2's polygon, 2026-10-08)."""
    hits = [m for m in candidates if _reaches(m, row)]
    if not hits:
        return None
    return min(
        hits,
        key=lambda m: (
            m.bounds is None or not m.bounds.contains(row.lat, row.lon),
            (row.type == "campus") != (m.kind in AREA_KINDS),
            not _same_name(row, m),
            not _same_org(row, m),
            haversine_m(row.lat, row.lon, m.lat, m.lon),
            ref_key(m.ref),
        ),
    )


def _name_match(row: PnnlRow, members: Sequence[OsmObject]) -> OsmObject | None:
    """The nearest member within NAME_RADIUS_M with the row's normalized name and an operator
    that does not disagree with the row's."""
    if not row.name:
        return None
    probe = OsmObject("node/0", row.lat, row.lon, None, {"operator": row.operator or ""}, "point")
    hits = [
        (haversine_m(row.lat, row.lon, m.lat, m.lon), m)
        for m in members
        if _same_name(row, m) and operators_compatible(probe, m)
    ]
    hits = [(d, m) for d, m in hits if d <= NAME_RADIUS_M]
    return min(hits, key=lambda h: (h[0], ref_key(h[1].ref)))[1] if hits else None


def _nearest(row: PnnlRow, members: Sequence[OsmObject]) -> tuple[str, float] | None:
    if not members:
        return None
    d, m = min(
        ((haversine_m(row.lat, row.lon, m.lat, m.lon), m) for m in members),
        key=lambda h: (h[0], ref_key(h[1].ref)),
    )
    return m.ref, round(d, 1)


def join(clusters: Sequence[Cluster], rows: Sequence[PnnlRow]) -> JoinResult:
    """Match PNNL rows to clusters: by OSM id when the row has one (point -> node, building and
    campus -> way, then relation), otherwise spatially (spatial_match), and failing that by name
    (_name_match). A row with an id that OSM no longer has is unmatched."""
    cluster_of: dict[str, Cluster] = {}
    for cluster in clusters:
        for m in cluster.members:
            cluster_of[m.ref] = cluster
    every = [m for c in clusters for m in c.members]
    grid = _Grid(every)
    matched: dict[str, list[PnnlRow]] = {}
    members: dict[str, str] = {}
    unmatched: list[PnnlRow] = []
    nearest: dict[str, tuple[str, float]] = {}
    by_id = by_name = 0
    for row in rows:
        member_ref: str | None = None
        if row.osm_id:
            member_ref = next((r for r in row.osm_refs() if r in cluster_of), None)
            if member_ref is not None:
                by_id += 1
        else:
            hit = spatial_match(row, grid.near(row.lat, row.lon))
            if hit is None and (hit := _name_match(row, every)) is not None:
                by_name += 1
            member_ref = hit.ref if hit is not None else None
        if member_ref is None:
            unmatched.append(row)
            if (found := _nearest(row, every)) is not None:
                nearest.setdefault(row.key, found)
            continue
        rep = cluster_of[member_ref].representative.ref
        matched.setdefault(rep, []).append(row)
        members.setdefault(row.key, member_ref)
    return JoinResult(
        matched=matched,
        unmatched=unmatched,
        members=members,
        by_id=by_id,
        by_name=by_name,
        nearest=nearest,
    )


# ---------------------------------------------------------------------------- effects


def pnnl_source(snapshot: InputSnapshot, source_id: str = "s2") -> Source:
    """The PNNL source entry of a matched record, for the file actually read.

    snapshot.upstream_version is the dataset version of that file when it is established (the
    MSD-LIVE CSV, or a web-map file in WEBMAP_VERSIONS); then the entry cites that version's DOI.
    Otherwise it cites the web-map file itself, without a version.
    """
    version = snapshot.upstream_version
    doi = VERSION_DOIS.get(version) if version is not None else None
    webmap = str(snapshot.url) == PNNL_GEOJSON_URL
    if doi is not None:
        url = doi
        title = f"{PNNL_DATASET} {version}" + (
            f", web map file {PNNL_WEBMAP_FILE}" if webmap else ""
        )
    else:
        url = str(snapshot.url)
        title = f"{PNNL_DATASET}, web map file {PNNL_WEBMAP_FILE}" if webmap else PNNL_DATASET
    return Source(
        id=source_id,
        url=HttpUrl(url),
        publisher=PNNL_PUBLISHER,
        title=title,
        source_type="open_dataset",
        license=PNNL_LICENSE,
        retrieved_at=snapshot.retrieved_at,
        supports=list(PNNL_SUPPORTS),
    )


def _unique(rows: Iterable[PnnlRow]) -> list[PnnlRow]:
    """One row per key: PNNL repeats a site that straddles a county line."""
    seen: dict[str, PnnlRow] = {}
    for row in rows:
        seen.setdefault(row.key, row)
    return list(seen.values())


@dataclass(frozen=True)
class SiteValues:
    """What a cluster's matched PNNL rows give its record, and the rows held back for review."""

    acreage: float | None = None  # site.acreage
    review: list[ReviewItem] = field(default_factory=list)


def _held(kind: str, reason: str, row: PnnlRow, member: OsmObject, rep: str) -> ReviewItem:
    return ReviewItem(
        source=REVIEW_SOURCE,
        kind=kind,
        external_id=row.key,
        record_id=None,
        reason=reason,
        data={"osm": member.ref, "representative": rep, "pnnl": row.to_json()},
    )


def _top_level(cluster: Cluster) -> list[OsmObject]:
    """The campus and site members that no larger campus or site member of the cluster covers."""
    areas = [m for m in cluster.members if m.kind in AREA_KINDS]
    return [
        m
        for m in areas
        if not any(
            o.ref != m.ref and o.area_m2() > m.area_m2() and o.covers(m.lat, m.lon) for o in areas
        )
    ]


def site_values(
    cluster: Cluster, rows: Sequence[PnnlRow], members: Mapping[str, str]
) -> SiteValues:
    """Acreage from the PNNL rows matched to a cluster, and review items (members: row key ->
    member ref, from JoinResult).

    - A building row checks the building or point it matched. When several rows hit one member
      (PNNL kept an old footprint next to the current one), the row with the member's name, else
      the nearest, is the match and the others are possible_duplicate items. A sqft more than
      FOOTPRINT_SLACK above the member's bounding box describes another footprint and is a
      conflict item. PNNL's sqft is the footprint's area, not a floor area, so no building row
      sets buildings[].sqft or site.building_sqft.
    - A campus row gives site.acreage only when it hit a campus or site object, and only when
      every campus or site member that no other one covers has such a row (a partial sum would
      read as the site's area). One whose campus polygon has left OpenStreetMap lands on a
      building, and is a conflict item. So is a row on a member whose position OpenStreetMap
      calls approximate, or whose area is more than FOOTPRINT_SLACK off the outline the import
      read (or above its bounding box, without an outline): then no acreage is set.

    Rows with the same key are counted once (PNNL repeats a site that straddles a county line).
    """
    rep = cluster.representative.ref
    by_ref = {m.ref: m for m in cluster.members}
    on_member: dict[str, list[tuple[PnnlRow, float]]] = {}
    campus_sqft: dict[str, float] = {}
    campus_rows: dict[str, list[PnnlRow]] = {}
    review: list[ReviewItem] = []
    for row in _unique(rows):
        ref = members.get(row.key)
        member = by_ref.get(ref) if ref is not None else None
        if member is None or row.sqft is None:
            continue
        if row.type == "campus" and member.kind in AREA_KINDS:
            campus_sqft[member.ref] = campus_sqft.get(member.ref, 0.0) + row.sqft
            campus_rows.setdefault(member.ref, []).append(row)
        elif row.type == "campus":
            reason = (
                f"PNNL campus row ({row.sqft / SQFT_PER_ACRE:.1f} acres) lies on {member.ref}, "
                f"a {member.kind}, not on a campus or site polygon in OpenStreetMap; acreage not "
                "applied"
            )
            review.append(_held("conflict", reason, row, member, rep))
        elif row.type == "building" and member.kind not in AREA_KINDS:
            on_member.setdefault(member.ref, []).append((row, row.sqft))
    for ref in sorted(on_member, key=ref_key):
        member = by_ref[ref]
        (kept, kept_sqft), *extra = sorted(
            on_member[ref],
            key=lambda item: (
                not _same_name(item[0], member),
                haversine_m(item[0].lat, item[0].lon, member.lat, member.lon),
                item[0].key,
            ),
        )
        for row, _ in extra:
            reason = (
                f"PNNL has {len(extra) + 1} building rows on {ref}; {kept.key} is the match (the "
                "same name, else the nearest) and this row is another footprint"
            )
            review.append(_held("possible_duplicate", reason, row, member, rep))
        box_sqft = member.area_m2() * SQFT_PER_M2
        if member.bounds is not None and kept_sqft > box_sqft * FOOTPRINT_SLACK:
            reason = (
                f"PNNL sqft {kept_sqft:,.0f} exceeds the bounding box of {ref} "
                f"({box_sqft:,.0f} sq ft) by more than {FOOTPRINT_SLACK - 1:.0%}: the row "
                "describes another footprint"
            )
            review.append(_held("conflict", reason, kept, member, rep))
    top = _top_level(cluster)
    acreage = None
    if top and all(m.ref in campus_sqft for m in top):
        doubts = {m.ref: why for m in top if (why := _campus_doubt(m, campus_sqft[m.ref]))}
        for ref in sorted(doubts, key=ref_key):
            for row in campus_rows[ref]:
                review.append(_held("conflict", doubts[ref], row, by_ref[ref], rep))
        if not doubts:
            acreage = round(sum(campus_sqft[m.ref] for m in top) / SQFT_PER_ACRE, 1) or None
    return SiteValues(acreage=acreage, review=review)


def _campus_doubt(member: OsmObject, sqft: float) -> str | None:
    """Why the PNNL campus rows on an area member do not give its acreage, or None: OpenStreetMap
    calls the member's position or boundary approximate (Meta Cheyenne: "location and boundary
    very approximate"), or the rows' area differs by more than FOOTPRINT_SLACK from the outline
    this run read (PNNL measured another version of the polygon), or, without an outline,
    exceeds the member's bounding box by more than that."""
    acres = sqft / SQFT_PER_ACRE
    note = member.approximate_note
    if note is not None:
        return (
            f"OpenStreetMap calls the position of {member.ref} approximate ({note[:80]!r}), so "
            f"its boundary does not give the site's area; the PNNL campus row ({acres:.1f} "
            "acres) is not applied"
        )
    row_m2 = sqft / SQFT_PER_M2
    outline_m2 = ring_area_m2(member.outline)
    slack = FOOTPRINT_SLACK - 1
    if outline_m2 is not None and outline_m2 > 0:
        if abs(row_m2 - outline_m2) > slack * outline_m2:
            outline_acres = outline_m2 * SQFT_PER_M2 / SQFT_PER_ACRE
            return (
                f"the PNNL campus row ({acres:.1f} acres) differs by more than {slack:.0%} from "
                f"the outline of {member.ref} this import read ({outline_acres:.1f} acres): it "
                "describes another version of the polygon; acreage not applied"
            )
        return None
    box_m2 = member.area_m2()
    if member.outline is None and member.bounds is not None and row_m2 > box_m2 * FOOTPRINT_SLACK:
        box_acres = box_m2 * SQFT_PER_M2 / SQFT_PER_ACRE
        return (
            f"the PNNL campus row ({acres:.1f} acres) exceeds the bounding box of {member.ref} "
            f"({box_acres:.1f} acres) by more than {slack:.0%}: it describes another polygon; "
            "acreage not applied"
        )
    return None


def county_mismatch(
    row: PnnlRow,
    *,
    county_fips: str | None,
    state_abbr: str,
    counties: CountyIndex,
) -> str | None:
    """Why the row's county disagrees with the record's point-in-polygon county, or None."""
    if county_fips is None or (row.county is None and row.county_id is None):
        return None
    if row.county_id is not None:
        if row.county_id == county_fips:
            return None
        return (
            f"PNNL county_id {row.county_id} differs from the point-in-polygon county {county_fips}"
        )
    abbr = row.state_abb or state_abbr
    county = counties.by_name(abbr, row.county or "")
    if county is not None and county.fips == county_fips:
        return None
    found = f"{county.name} ({county.fips})" if county is not None else "no such county"
    return (
        f"PNNL county {row.county!r}, {abbr} ({found}) differs from the point-in-polygon "
        f"county {county_fips}"
    )


def county_mismatches(
    rows: Sequence[PnnlRow],
    *,
    county_fips: str | None,
    state_abbr: str,
    counties: CountyIndex,
) -> list[tuple[PnnlRow, str]]:
    """(row, reason) for each row key whose every row disagrees with the record's county.

    PNNL lists a site that straddles a county line once per county, under one key, so a key
    agrees when any of its rows does.
    """
    by_key: dict[str, list[PnnlRow]] = {}
    for row in rows:
        by_key.setdefault(row.key, []).append(row)
    out: list[tuple[PnnlRow, str]] = []
    for same_key in by_key.values():
        found = [
            (row, why)
            for row in same_key
            if (
                why := county_mismatch(
                    row, county_fips=county_fips, state_abbr=state_abbr, counties=counties
                )
            )
            is not None
        ]
        if len(found) == len(same_key):
            out.append(found[0])
    return out


def unmatched_item(row: PnnlRow, nearest: tuple[str, float] | None = None) -> ReviewItem:
    """The review item for a row no OSM-derived record took. The import reads only data center
    elements (atlas.sources.osm.OVERPASS_QUERY), so OSM may still have the feature, retagged
    (telecom=exchange, office=*) or moved; nearest names the closest element the import read."""
    what = f"PNNL {row.type} row" if row.type != "point" else "PNNL point"
    if row.osm_id:
        reason = (
            f"the {what}'s {' or '.join(row.osm_refs())} is not among the OSM data center "
            f"elements this import read (PNNL {PNNL_VERSION}): deleted, or retagged"
        )
    else:
        reason = (
            f"no OSM data center element this import read takes the {what}: no bounding box "
            f"(+{CONTAIN_MARGIN_M:g} m) holds its point, no center is within {NEAR_RADIUS_M:g} m "
            f"and no element of its name is within {NAME_RADIUS_M:g} m"
        )
    data = row.to_json()
    if nearest is not None:
        reason += f"; the nearest is {nearest[0]}, {nearest[1]:,.0f} m away"
        data |= {"nearest_osm": nearest[0], "nearest_m": nearest[1]}
    return ReviewItem(
        source=REVIEW_SOURCE,
        kind="unmatched",
        external_id=row.key,
        record_id=None,
        reason=reason,
        data=data,
    )


# ---------------------------------------------------------------------------- input


def msdlive_version(
    ctx: ImportContext, *, sleep: Callable[[float], None] = time.sleep
) -> str | None:
    """metadata.version of the latest MSD-LIVE record version, or None (with a warning). It is
    only compared with PNNL_VERSION; it never says which version a web-map file is.

    The records API is a JSON API, read once per run, so robots.txt is not consulted (as for
    Overpass); its robots.txt answered 502 on 2026-10-07. The data files are never requested.
    """
    url = f"{MSDLIVE_RECORD}/versions/latest"
    try:
        result = fetch(
            ctx.http,
            url,
            robots=False,
            allowed_types=("application/json",),
            max_bytes=2_000_000,
            retries=1,
            sleep=sleep,
        )
        doc = json.loads(result.content)
        version = doc["metadata"]["version"]
        if not isinstance(version, str) or not version.strip():
            raise ValueError("metadata.version is not a string")
    except (FetchError, ValueError, KeyError, TypeError) as e:
        print(f"warning: MSD-LIVE version check failed: {e}", file=sys.stderr)
        return None
    return version.strip()


def _webmap_snapshot(snapshot: InputSnapshot, latest: str | None) -> InputSnapshot:
    """The web-map snapshot with upstream_version = the file's established dataset version (or
    None), and warnings when that is unknown or MSD-LIVE lists a newer version."""
    version = WEBMAP_VERSIONS.get(snapshot.sha256)
    if version is None:
        print(
            f"warning: the PNNL web-map file (sha256 {snapshot.sha256[:12]}) is not the one "
            f"checked as {PNNL_VERSION}; records cite the file, without a dataset version",
            file=sys.stderr,
        )
    if latest is not None and latest != PNNL_VERSION:
        print(
            f"warning: MSD-LIVE lists PNNL {latest}; this importer was checked against "
            f"{PNNL_VERSION}, and records cite the version of the file read "
            f"({version or 'not established'})",
            file=sys.stderr,
        )
    return snapshot.model_copy(update={"upstream_version": version})


def load_pnnl_input(
    ctx: ImportContext,
    path: Path | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[PnnlRow], InputSnapshot]:
    """PNNL rows and their snapshot: from path (--pnnl), or the public GeoJSON over HTTP."""
    if path is not None:

        def no_fetch() -> FetchResult:  # load_input reads path; it never calls this
            raise FetchError("unreachable: --pnnl is a local file")

        data, snapshot = load_input(
            ctx,
            name=REVIEW_SOURCE,
            url=PNNL_GEOJSON_URL,
            license=PNNL_LICENSE,
            ext=path.suffix.lstrip(".") or "geojson",
            fetch=no_fetch,
            path=path,
            use_ctx_input=False,
        )
        rows = parse_pnnl(data)
        if any(r.osm_id or r.county_id for r in rows):  # the MSD-LIVE v2026.02.09 layout
            update = {"url": HttpUrl(PNNL_DOI_URL), "upstream_version": PNNL_VERSION}
            return rows, snapshot.model_copy(update=update)
        return rows, _webmap_snapshot(snapshot, None)

    latest = msdlive_version(ctx, sleep=sleep)

    def get() -> FetchResult:
        return fetch(
            ctx.http,
            PNNL_GEOJSON_URL,
            allowed_types=("application/geo+json", "application/json"),
            max_bytes=20_000_000,
            sleep=sleep,
        )

    data, snapshot = load_input(
        ctx,
        name=REVIEW_SOURCE,
        url=PNNL_GEOJSON_URL,
        license=PNNL_LICENSE,
        ext="geojson",
        fetch=get,
        use_ctx_input=False,
    )
    return parse_pnnl(data), _webmap_snapshot(snapshot, latest)
