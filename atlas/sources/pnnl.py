"""PNNL IM3 Open Source Data Center Atlas: the cross-check for the OSM seed (07 §4.1, §4.2 step 3).

PNNL v2026.02.09 (DOI 10.57931/3017294, ODbL 1.0) is derived from OpenStreetMap. Two layouts are
read:

- the public web-map file (PNNL_GEOJSON_URL): 1,382 Point features whose properties are only
  state_abb, county, operator, name, sqft and type. It has no OSM id and no FIPS code.
- a CSV in the MSD-LIVE column layout (id, state, state_abb, state_id, county, county_id, ref,
  operator, name, sqft, lat, lon, type), if the owner mirrors the MSD-LIVE files. Those files need
  an MSD-LIVE sign-in, so this module never downloads them; MSD-LIVE is used only for a version
  check through its public records API.

join() matches rows to dissolved OSM clusters: by OSM id when a row has one, otherwise spatially.
The OSM importer (atlas.sources.osm) applies the matches to its records. This module has no
IMPORTER: PNNL rows only enrich and cross-check OSM records.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import HttpUrl, JsonValue

from atlas.dissolve import Cluster, OsmObject, haversine_m, meters_to_degrees, ref_key
from atlas.net import FetchError, FetchResult, fetch
from atlas.schema.record import Source
from atlas.sources.base import ImportContext, InputSnapshot, ReviewItem, load_input
from atlas.text import normalize_name, normalize_org, strip_invisible

if TYPE_CHECKING:
    from atlas.geo.counties import CountyIndex

PNNL_GEOJSON_URL = "https://immm-sfa.github.io/datacenter-atlas/im3_datacenter_centroids.geojson"
MSDLIVE_RECORD = "https://data.msdlive.org/api/records/p147s-4h760"
PNNL_DOI_URL = "https://doi.org/10.57931/3017294"
PNNL_VERSION = "v2026.02.09"
PNNL_LICENSE = "ODbL-1.0"
PNNL_PUBLISHER = "Pacific Northwest National Laboratory (IM3)"
PNNL_TITLE = f"IM3 Open Source Data Center Atlas {PNNL_VERSION}"
PNNL_SUPPORTS = ("/site", "/buildings")
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
SQFT_PER_ACRE = 43_560.0
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
    text = " ".join(strip_invisible(str(value)).split())
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
    """matched: representative ref -> rows (in input order); members: row key -> member ref."""

    matched: dict[str, list[PnnlRow]]
    unmatched: list[PnnlRow]
    members: dict[str, str] = field(default_factory=dict)
    by_id: int = 0

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
    50 m. Ties break on the same normalized name, then the same normalized operator, then
    distance, then ref."""
    hits = [m for m in candidates if _reaches(m, row)]
    if not hits:
        return None
    return min(
        hits,
        key=lambda m: (
            not _same_name(row, m),
            not _same_org(row, m),
            haversine_m(row.lat, row.lon, m.lat, m.lon),
            ref_key(m.ref),
        ),
    )


def join(clusters: Sequence[Cluster], rows: Sequence[PnnlRow]) -> JoinResult:
    """Match PNNL rows to clusters: by OSM id when the row has one (point -> node, building and
    campus -> way, then relation), otherwise spatially (spatial_match). A row with an id that OSM
    no longer has is unmatched."""
    cluster_of: dict[str, Cluster] = {}
    for cluster in clusters:
        for m in cluster.members:
            cluster_of[m.ref] = cluster
    grid = _Grid(m for c in clusters for m in c.members)
    matched: dict[str, list[PnnlRow]] = {}
    members: dict[str, str] = {}
    unmatched: list[PnnlRow] = []
    by_id = 0
    for row in rows:
        member_ref: str | None = None
        if row.osm_id:
            member_ref = next((r for r in row.osm_refs() if r in cluster_of), None)
            if member_ref is not None:
                by_id += 1
        else:
            hit = spatial_match(row, grid.near(row.lat, row.lon))
            member_ref = hit.ref if hit is not None else None
        if member_ref is None:
            unmatched.append(row)
            continue
        rep = cluster_of[member_ref].representative.ref
        matched.setdefault(rep, []).append(row)
        members.setdefault(row.key, member_ref)
    return JoinResult(matched=matched, unmatched=unmatched, members=members, by_id=by_id)


# ---------------------------------------------------------------------------- effects


def pnnl_source(retrieved_at: datetime, source_id: str = "s2") -> Source:
    """The PNNL source entry of a matched record."""
    return Source(
        id=source_id,
        url=HttpUrl(PNNL_DOI_URL),
        publisher=PNNL_PUBLISHER,
        title=PNNL_TITLE,
        source_type="open_dataset",
        license=PNNL_LICENSE,
        retrieved_at=retrieved_at,
        supports=list(PNNL_SUPPORTS),
    )


def _unique(rows: Iterable[PnnlRow]) -> list[PnnlRow]:
    """One row per key: PNNL repeats a site that straddles a county line."""
    seen: dict[str, PnnlRow] = {}
    for row in rows:
        seen.setdefault(row.key, row)
    return list(seen.values())


def building_sqft(rows: Sequence[PnnlRow], members: dict[str, str]) -> dict[str, float]:
    """Floor area per member ref, from building rows (summed when several rows hit one member)."""
    out: dict[str, float] = {}
    for row in _unique(rows):
        ref = members.get(row.key)
        if row.type == "building" and row.sqft is not None and ref is not None:
            out[ref] = out.get(ref, 0.0) + row.sqft
    return out


def site_values(rows: Sequence[PnnlRow]) -> tuple[float | None, float | None]:
    """(building_sqft, acreage): building rows' sqft summed, and campus rows' area in acres."""
    unique = _unique(rows)
    sqft = [r.sqft for r in unique if r.type == "building" and r.sqft is not None]
    campus = [r.sqft for r in unique if r.type == "campus" and r.sqft is not None]
    building_total = round(sum(sqft), 1) if sqft else None
    acreage = round(sum(campus) / SQFT_PER_ACRE, 1) if campus else None
    return building_total, (acreage if acreage else None)


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


def unmatched_item(row: PnnlRow) -> ReviewItem:
    if row.osm_id:
        reason = f"OSM no longer has {' or '.join(row.osm_refs())} (PNNL {PNNL_VERSION})"
    else:
        reason = (
            f"no OSM object contains this PNNL {row.type} point or lies within "
            f"{NEAR_RADIUS_M:g} m of it"
        )
    return ReviewItem(
        source=REVIEW_SOURCE,
        kind="unmatched",
        external_id=row.key,
        record_id=None,
        reason=reason,
        data=row.to_json(),
    )


# ---------------------------------------------------------------------------- input


def msdlive_version(
    ctx: ImportContext, *, sleep: Callable[[float], None] = time.sleep
) -> str | None:
    """metadata.version of the latest MSD-LIVE record version, or None (with a warning).

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
    version = version.strip()
    if version != PNNL_VERSION:
        print(
            f"warning: MSD-LIVE lists PNNL {version}; this importer was checked against "
            f"{PNNL_VERSION}",
            file=sys.stderr,
        )
    return version


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
        if any(r.osm_id or r.county_id for r in rows):  # the MSD-LIVE layout: cite the DOI
            snapshot = snapshot.model_copy(update={"url": HttpUrl(PNNL_DOI_URL)})
        return rows, snapshot

    version = msdlive_version(ctx, sleep=sleep)

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
        upstream_version=version,
        use_ctx_input=False,
    )
    return parse_pnnl(data), snapshot
