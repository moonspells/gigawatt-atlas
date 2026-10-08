"""The release builder (07 §6.8 steps 1-3, §11; contract_version 1).

build_release turns data/records into one release directory whose layout mirrors the R2 keys of
atlas-tiles:

    v/{release}/manifest.json            files with bytes and SHA-256, counts, git, layers
    v/{release}/facilities.parquet       GeoParquet 1.1 (WKB points, bbox covering, zstd)
    v/{release}/facilities.geojson       full download
    v/{release}/facilities.csv           flat download
    v/{release}/facilities.pmtiles       tippecanoe, for downloads and third-party maps
    v/{release}/facilities-map.json      compact GeoJSON for the /atlas map and table
    v/{release}/records.jsonl.gz         every public record (merged ones too), sorted by id
    v/{release}/summary.json             counts and GW by status, group, state, ISO, evidence
    v/{release}/feed.json                merged changes of the last 90 days (empty until M2)
    v/{release}/schema/facility.v1.json  the JSON Schema
    v/{release}/LICENSE-ODbL-1.0.txt ATTRIBUTION.md README.md CHANGELOG.md
    rec/{id}/{sha256[:12]}.json          one public record, content-addressed
    atlas/latest.json                    the pointer, written last (never for the fixture)

Public records are the in-scope ones with invisible characters stripped from every string.
Release checks: atlas validate, the record count against the previous release (±10%), the point
bounds, ST_IsValid, the size budgets and the upload allow-list. A release is built in a temporary
sibling of the output directory and moved in only when every check has passed. Nothing here opens
an R2 connection: upload_release and the takedown (plan_takedown, run_takedown; 07 §5.4) act
through the atlas.r2 Uploader or Bucket they are given.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_args

from pydantic import JsonValue

from atlas.jsonio import dumps_compact, dumps_pretty, read_json, record_json
from atlas.r2 import (
    LATEST_KEY,
    Bucket,
    DisallowedKey,
    Uploader,
    UploadReport,
    cache_control_for,
    content_type_for,
    plan_release,
    upload_item,
)
from atlas.schema.export import SCHEMA_PATH, render
from atlas.schema.record import (
    SCHEMA_VERSION,
    FacilityRecord,
    OrgRef,
    Status,
)
from atlas.schema.rollup import is_expanding, latest_event, mw_display, status_group
from atlas.store import RecordStore, load_orgs
from atlas.text import strip_invisible

if TYPE_CHECKING:
    import httpx

    from atlas.geo.counties import CountyIndex

REPO_ROOT = Path(__file__).resolve().parents[1]

CONTRACT_VERSION = 1
RELEASE_RE = re.compile(r"^\d{8}-\d{4}$")
FIXTURE_RELEASE = "20000101-0000"
DEFAULT_TILES_BASE = "https://tiles.moonspells.dev"
DEFAULT_PREVIOUS = f"{DEFAULT_TILES_BASE}/atlas/latest.json"
DEFAULT_LAYERS = Path("overlays/out/layers.json")
DEFAULT_ATTRIBUTION = Path("ATTRIBUTION.md")
DEFAULT_CHANGELOG = Path("CHANGELOG.md")
DEFAULT_OUT = Path("build/release")
FIXTURE_OUT = Path("fixtures/release")
FIXTURE_RECORDS = Path("tests/fixtures/records")
FIXTURE_ORGS = Path("tests/fixtures/orgs.json")
# Frozen inputs of the fixture release: changed only on purpose, together with fixtures/release.
FIXTURE_INPUTS = Path("fixtures/release-inputs")
LICENSE_ID = "ODbL-1.0"
FACILITY_URL = "https://moonspells.dev/atlas/facility/{id}/"
ATTRIBUTION = (
    "Gigawatt Atlas, moonspells.dev/atlas, release {release}, ODbL 1.0; contains information "
    "from OpenStreetMap contributors (ODbL), PNNL IM3 (ODbL), Epoch AI (CC BY 4.0), "
    "AI GridWatch (CC BY 4.0)"
)
FEED_WINDOW_DAYS = 90

STATUSES: tuple[Status, ...] = get_args(Status)
GROUPS = ("op", "uc", "pl", "cx", "pa")
EVIDENCE_LEVELS = ("confirmed", "reported", "rumor")

# facilities-map.json feature properties (07 §11.1); exactly these keys, in contract 1.
MAP_KEYS = ("id", "n", "a", "s", "g", "m", "b", "p", "o", "ot", "st", "c", "e", "t", "h", "x")
# facilities.csv columns; also the GeoJSON properties and the flat Parquet columns.
CSV_COLUMNS = (
    "id",
    "name",
    "record_type",
    "status",
    "status_group",
    "evidence_level",
    "operator",
    "owner",
    "developer",
    "tenant",
    "purpose",
    "state",
    "county_fips",
    "county_name",
    "city",
    "lat",
    "lon",
    "precision",
    "it_mw",
    "facility_mw",
    "utility_request_mw",
    "mw_display",
    "mw_basis",
    "mw_as_stated",
    "investment_usd",
    "investment_basis",
    "acreage",
    "building_sqft",
    "iso_rto",
    "utility_name",
    "announced",
    "application_filed",
    "approved",
    "construction_start",
    "expected_in_service",
    "operating_since",
    "latest_event",
    "latest_event_date",
    "expanding",
    "n_sources",
    "confidence_status",
    "review_state",
    "updated_at",
    "url",
)
_DOUBLE_COLUMNS = frozenset(
    {
        "lat",
        "lon",
        "it_mw",
        "facility_mw",
        "utility_request_mw",
        "mw_display",
        "investment_usd",
        "acreage",
        "building_sqft",
        "confidence_status",
    }
)
JSON_COLUMNS = (
    "aliases",
    "parties",
    "status_history",
    "phases",
    "buildings",
    "sources",
    "incentives",
)
# First characters that make a spreadsheet read a CSV cell as a formula (see csv_text_cell).
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")
DATE_COLUMNS = (
    "announced",
    "application_filed",
    "approved",
    "construction_start",
    "expected_in_service",
    "operating_since",
)

# Budgets and bounds (07 §6.8 step 1, §11.1, §12.6).
MAP_BUDGET_GZIP_BYTES = 400_000
REC_MAX_BYTES = 65_536
FILE_MAX_BYTES = 500_000_000  # under Cloudflare's 512 MB cacheable-object limit on Free
COUNT_TOLERANCE = 0.10
LAT_BOUNDS = (18.0, 72.0)
LON_BOUNDS = (-180.0, -64.0)

# The archive's own attribution: facilities.pmtiles is for downloads and third-party maps, and its
# features carry every flat column (names, MW, dates from all four sources), so a map that shows
# only the archive's attribution must still credit each source, the CC BY 4.0 ones included.
TILES_ATTRIBUTION = (
    "Gigawatt Atlas (ODbL) · © OpenStreetMap contributors · PNNL IM3 (ODbL) · "
    "Epoch AI (CC BY 4.0) · AI GridWatch (CC BY 4.0)"
)
TIPPECANOE_ARGS = (
    "-l",
    "facilities",
    "-Z0",
    "-z12",
    "-r1",
    "--no-feature-limit",
    "--no-tile-size-limit",
    "-n",
    "Gigawatt Atlas facilities",
    "-A",
    TILES_ATTRIBUTION,
    "--force",
)

# Files compared by content rather than bytes in `atlas publish fixture --check`.
BINARY_COMPARED = (".parquet", ".pmtiles")
RELEASE_ROOTS = ("v", "rec", "atlas")
_REC_KEY_RE = re.compile(r"^rec/(gwa-[0-9a-hjkmnp-tv-z]{26})/([0-9a-f]{12})\.json$")


class ReleaseError(Exception):
    """A release check failed; problems lists each failure."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems[:5]))


# ------------------------------------------------------------------------- small helpers


def release_now(now: datetime | None = None) -> str:
    """The release id for a UTC time: YYYYMMDD-HHMM."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d-%H%M")


def release_time(release: str) -> datetime:
    """The UTC time a release id stands for. Raises ValueError for a malformed id."""
    if not RELEASE_RE.match(release):
        raise ValueError(f"release id must be YYYYMMDD-HHMM (UTC), got {release!r}")
    return datetime.strptime(release, "%Y%m%d-%H%M").replace(tzinfo=UTC)


def iso_z(dt: datetime) -> str:
    """2026-10-12T12:00:00Z (UTC, whole seconds)."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gzip_bytes(data: bytes) -> bytes:
    """Reproducible gzip: mtime 0, no file name, level 9."""
    return gzip.compress(data, compresslevel=9, mtime=0)


def _strip(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return strip_invisible(value)
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, dict):
        return {strip_invisible(k): _strip(v) for k, v in value.items()}
    return value


def public_record(record: FacilityRecord) -> FacilityRecord:
    """The published form of an in-scope record: every string without invisible characters."""
    if record.scope != "in_scope":
        raise ValueError(f"{record.id} is out of scope and is never published")
    return FacilityRecord.model_validate(_strip(record_json(record)))


def rec_object(record: FacilityRecord) -> tuple[bytes, str]:
    """The bytes of rec/{id}/{h}.json for a public record, and h (the SHA-256 prefix)."""
    data = dumps_compact(record_json(record))
    return data, sha256_bytes(data)[:12]


def _names(refs: Iterable[OrgRef]) -> str | None:
    return " | ".join(ref.name for ref in refs) or None


def _lonlat(record: FacilityRecord) -> tuple[float, float] | None:
    loc = record.location
    if loc.lat is None or loc.lon is None:
        return None
    return round(loc.lon, 6), round(loc.lat, 6)


def _resolve(path: Path, root: Path) -> Path:
    """A relative path that does not exist here is looked up under the repo root."""
    if path.is_absolute() or path.exists():
        return path
    candidate = root / path
    return candidate if candidate.exists() else path


# ------------------------------------------------------------------------- facility rows


@dataclass(frozen=True)
class Facility:
    """One published facility: the public record, its rec/ object and its flat row."""

    record: FacilityRecord
    rec_bytes: bytes
    h: str
    row: dict[str, JsonValue]
    lonlat: tuple[float, float] | None

    @property
    def rec_key(self) -> str:
        return f"rec/{self.record.id}/{self.h}.json"

    def geometry(self) -> JsonValue:
        if self.lonlat is None:
            return None
        return {"type": "Point", "coordinates": [self.lonlat[0], self.lonlat[1]]}


def flat_row(record: FacilityRecord, doc: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The CSV columns of a public record (doc is its JSON dump)."""
    loc = record.location
    cap = record.capacity
    mw, basis = mw_display(cap)
    event = latest_event(record)
    lonlat = _lonlat(record)
    status_meta = record.field_meta.get("/status")
    row: dict[str, JsonValue] = {
        "id": record.id,
        "name": record.canonical_name,
        "record_type": record.record_type,
        "status": record.status,
        "status_group": status_group(record.status),
        "evidence_level": record.evidence_level,
        "operator": _names(record.parties.operator),
        "owner": _names(record.parties.owner),
        "developer": _names(record.parties.developer),
        "tenant": _names(record.parties.tenant),
        "purpose": record.purpose,
        "state": loc.state_abbr,
        "county_fips": loc.county_fips,
        "county_name": loc.county_name,
        "city": loc.city,
        "lat": lonlat[1] if lonlat else None,
        "lon": lonlat[0] if lonlat else None,
        "precision": loc.precision,
        "it_mw": cap.it_mw,
        "facility_mw": cap.facility_mw,
        "utility_request_mw": cap.utility_request_mw,
        "mw_display": mw,
        "mw_basis": basis,
        "mw_as_stated": cap.mw_as_stated,
        "investment_usd": record.money.investment_usd,
        "investment_basis": record.money.investment_basis,
        "acreage": record.site.acreage,
        "building_sqft": record.site.building_sqft,
        "iso_rto": record.grid.iso_rto,
        "utility_name": record.grid.utility_name,
        "latest_event": event.event,
        "latest_event_date": event.as_of.value,
        "expanding": is_expanding(record),
        "n_sources": len(record.sources),
        "confidence_status": status_meta.confidence if status_meta else None,
        "review_state": record.review.state,
        "updated_at": doc["updated_at"],
        "url": FACILITY_URL.format(id=record.id),
    }
    for key in DATE_COLUMNS:
        d = record.dates.get(key)
        row[key] = d.value if d else None
    return {column: row[column] for column in CSV_COLUMNS}


def make_facility(record: FacilityRecord) -> Facility:
    """record must already be public (see public_record)."""
    data, h = rec_object(record)
    doc = cast("dict[str, JsonValue]", json.loads(data))
    return Facility(record, data, h, flat_row(record, doc), _lonlat(record))


def map_properties(f: Facility, org_kinds: Mapping[str, str]) -> dict[str, JsonValue]:
    """The 16 short keys of a facilities-map.json feature (07 §11.1)."""
    r = f.record
    mw, basis = mw_display(r.capacity)
    operator = r.parties.operator[0] if r.parties.operator else None
    kind = org_kinds.get(operator.org_id) if operator and operator.org_id else None
    props: dict[str, JsonValue] = {
        "id": r.id,
        "n": r.canonical_name,
        "a": " | ".join(a.name for a in r.aliases) or None,
        "s": r.status,
        "g": status_group(r.status),
        "m": mw,
        "b": basis,
        "p": r.location.precision,
        "o": operator.name if operator else None,
        "ot": kind,
        "st": r.location.state_abbr,
        "c": r.location.county_fips,
        "e": r.evidence_level,
        "t": latest_event(r).as_of.value,
        "h": f.h,
        "x": is_expanding(r),
    }
    return {key: props[key] for key in MAP_KEYS}


# ------------------------------------------------------------------------ file renderers


def _by_id(facilities: Sequence[Facility]) -> list[Facility]:
    return sorted(facilities, key=lambda f: f.record.id)


def csv_text_cell(value: str) -> str:
    """A text cell that spreadsheets will not run as a formula (OWASP CSV injection).

    Names and parties come from untrusted sources (OSM tags anyone can edit, trackers, later LLM
    extraction). Excel, Sheets and LibreOffice treat a cell that starts with = + - @, a tab or a
    carriage return (or their full-width forms) as a formula, so such a cell gets a leading
    apostrophe. Only text goes through here; numbers such as lon -77.48 stay as they are.
    """
    return "'" + value if value.startswith(CSV_FORMULA_PREFIXES) else value


def render_csv(facilities: Sequence[Facility]) -> bytes:
    def cell(value: JsonValue) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return csv_text_cell(value)
        return str(value)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_COLUMNS)
    for f in _by_id(facilities):
        writer.writerow([cell(f.row[c]) for c in CSV_COLUMNS])
    return buffer.getvalue().encode("utf-8")


def render_geojson(facilities: Sequence[Facility]) -> bytes:
    features: list[JsonValue] = [
        {"type": "Feature", "properties": dict(f.row), "geometry": f.geometry()}
        for f in _by_id(facilities)
    ]
    return dumps_compact({"type": "FeatureCollection", "features": features})


def render_map(release: str, facilities: Sequence[Facility], org_kinds: Mapping[str, str]) -> bytes:
    features: list[JsonValue] = [
        {"type": "Feature", "properties": map_properties(f, org_kinds), "geometry": f.geometry()}
        for f in _by_id(facilities)
    ]
    return dumps_compact({"type": "FeatureCollection", "release": release, "features": features})


def render_records(records: Sequence[FacilityRecord]) -> bytes:
    lines = [dumps_compact(record_json(r)) for r in sorted(records, key=lambda r: r.id)]
    return gzip_bytes(b"".join(line + b"\n" for line in lines))


def _gw(mw: float) -> float:
    return round(mw / 1000, 3)


def build_summary(
    release: str, generated_at: str, facilities: Sequence[Facility], merged: int
) -> dict[str, JsonValue]:
    """summary.json: counts and GW (sum of mw_display / 1000, 3 decimals)."""
    by_status = {s: [0, 0.0] for s in STATUSES}
    by_group = {g: [0, 0.0] for g in GROUPS}
    by_evidence = dict.fromkeys(EVIDENCE_LEVELS, 0)
    by_state: dict[str, dict[str, Any]] = {}
    by_iso: dict[str, list[float]] = {}
    total = utility_basis = 0.0
    without_mw = unmappable = 0
    last_updated: datetime | None = None
    for f in facilities:
        r = f.record
        mw, basis = mw_display(r.capacity)
        value = mw or 0.0
        if mw is None:
            without_mw += 1
        if basis == "utility_request":
            utility_basis += value
        if f.lonlat is None:
            unmappable += 1
        total += value
        by_status[r.status][0] += 1
        by_status[r.status][1] += value
        group = status_group(r.status)
        by_group[group][0] += 1
        by_group[group][1] += value
        by_evidence[r.evidence_level] += 1
        st = by_state.setdefault(
            r.location.state_abbr, {"count": 0, "mw": 0.0, "by_status": dict.fromkeys(STATUSES, 0)}
        )
        st["count"] += 1
        st["mw"] += value
        st["by_status"][r.status] += 1
        iso = by_iso.setdefault(r.grid.iso_rto, [0, 0.0])
        iso[0] += 1
        iso[1] += value
        # Compared as datetimes: updated_at may carry any UTC offset, so strings sort wrongly.
        if last_updated is None or r.updated_at > last_updated:
            last_updated = r.updated_at
    return {
        "release": release,
        "generated_at": generated_at,
        "facilities": len(facilities),
        "merged": merged,
        "without_mw": without_mw,
        "unmappable": unmappable,
        "gw_total": _gw(total),
        "gw_utility_request_basis": _gw(utility_basis),
        "by_status": {s: {"count": int(c), "gw": _gw(m)} for s, (c, m) in by_status.items()},
        "by_group": {g: {"count": int(c), "gw": _gw(m)} for g, (c, m) in by_group.items()},
        "by_state": {
            abbr: {"count": v["count"], "gw": _gw(v["mw"]), "by_status": v["by_status"]}
            for abbr, v in sorted(by_state.items())
        },
        "by_iso": {iso: {"count": int(c), "gw": _gw(m)} for iso, (c, m) in sorted(by_iso.items())},
        "by_evidence": cast("dict[str, JsonValue]", by_evidence),
        "last_updated": iso_z(last_updated) if last_updated else None,
    }


FILE_DESCRIPTIONS = {
    "manifest.json": "every file with bytes and SHA-256, record counts, git commit, layers",
    "facilities.parquet": "GeoParquet 1.1: one row per facility, nested parts as JSON columns",
    "facilities.geojson": "GeoJSON FeatureCollection, the flat columns as properties",
    "facilities.csv": "flat table, UTF-8, one row per facility",
    "facilities.pmtiles": "vector tiles (layer `facilities`, zoom 0-12) for third-party maps",
    "facilities-map.json": "compact GeoJSON with short keys, used by the /atlas map and table",
    "records.jsonl.gz": "every public record in full (merged records too), one JSON per line",
    "summary.json": "counts and GW by status, group, state, ISO/RTO and evidence level",
    "feed.json": "changes merged in the last 90 days",
    "schema/facility.v1.json": "JSON Schema (draft 2020-12) of one record",
    "LICENSE-ODbL-1.0.txt": "the Open Database License 1.0",
    "ATTRIBUTION.md": "every source, its license and citation",
    "README.md": "this file",
    "CHANGELOG.md": "the data changelog",
}


def render_readme(release: str, files: Sequence[str], *, fixture: bool) -> str:
    lines = [f"# Gigawatt Atlas release {release}", ""]
    lines += [
        "The Gigawatt Atlas is an open dataset of US data center campuses and projects:",
        "operating, under construction and proposed. Map and methodology:",
        "<https://moonspells.dev/atlas>. Pipeline and records:",
        "<https://github.com/moonspells/gigawatt-atlas>.",
        "",
    ]
    if fixture:
        lines += [
            "**This is the fixture release.** Its records are test data for contract tests and",
            "site CI, mostly invented examples. Do not use or cite them as facts about real",
            "facilities.",
            "",
        ]
    lines += ["## Files", "", "| File | What it holds |", "|---|---|"]
    for name in files:
        lines.append(f"| `{name}` | {FILE_DESCRIPTIONS.get(name, '')} |")
    lines += [
        "",
        "Each record also has its own object at `rec/{id}/{hash}.json` next to `v/`; the hash is",
        "the `h` property in `facilities-map.json`. `manifest.json` lists the bytes and SHA-256 of",
        "every other file above.",
        "",
        "## License in plain words",
        "",
        "The database is published under the Open Database License 1.0 (`LICENSE-ODbL-1.0.txt`,",
        "which is the binding text). You may use, copy and build on it, commercially or not, if you:",
        "",
        "1. credit the Gigawatt Atlas and its sources (the line below, and `ATTRIBUTION.md`);",
        "2. share alike: offer any database you derive from it and use or distribute publicly",
        "   under the ODbL too;",
        "3. keep it open: do not add technical restrictions without also offering an",
        "   unrestricted copy.",
        "",
        "Maps, charts and screenshots made from the data are Produced Works: they need the",
        "attribution, not the share-alike.",
        "",
        "## Attribution",
        "",
        "> " + ATTRIBUTION.format(release=release),
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- GeoParquet


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _parquet_columns() -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    for name in CSV_COLUMNS:
        if name in _DOUBLE_COLUMNS:
            kind = "DOUBLE"
        elif name == "expanding":
            kind = "BOOLEAN"
        elif name == "n_sources":
            kind = "INTEGER"
        else:
            kind = "VARCHAR"
        columns.append((name, kind))
    columns += [(name, "VARCHAR") for name in JSON_COLUMNS]
    return columns


def geo_metadata(facilities: Sequence[Facility]) -> dict[str, Any]:
    """GeoParquet 1.1.0 'geo' metadata with a bbox covering; no crs key (OGC:CRS84)."""
    column: dict[str, Any] = {"encoding": "WKB", "geometry_types": ["Point"]}
    points = [f.lonlat for f in facilities if f.lonlat is not None]
    if points:
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        column["bbox"] = [min(lons), min(lats), max(lons), max(lats)]
    column["covering"] = {
        "bbox": {
            "xmin": ["bbox", "xmin"],
            "ymin": ["bbox", "ymin"],
            "xmax": ["bbox", "xmax"],
            "ymax": ["bbox", "ymax"],
        }
    }
    return {"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": column}}


_PARQUET_SELECT = (
    "SELECT *, "
    "CASE WHEN lat IS NULL OR lon IS NULL THEN NULL "
    "ELSE ST_AsWKB(ST_Point(lon, lat)) END AS geometry, "
    "CASE WHEN lat IS NULL OR lon IS NULL THEN NULL ELSE struct_pack("
    "xmin := lon::DOUBLE, ymin := lat::DOUBLE, xmax := lon::DOUBLE, ymax := lat::DOUBLE"
    ") END AS bbox FROM facilities ORDER BY id"
)
_PARQUET_CHECK = (
    "SELECT count(*), count(*) FILTER (WHERE geometry IS NOT NULL AND NOT ST_IsValid(geometry)) "
    "FROM read_parquet(?)"
)
_PARQUET_ROWS = (
    "SELECT * EXCLUDE (geometry), ST_AsText(geometry) AS geometry_wkt "
    "FROM read_parquet(?) ORDER BY id"
)


def write_parquet(path: Path, facilities: Sequence[Facility]) -> tuple[int, int]:
    """Write facilities.parquet with DuckDB; return (rows read back, invalid geometries)."""
    from atlas.geo.duck import connect  # DuckDB is imported on use

    columns = _parquet_columns()
    lines: list[bytes] = []
    for f in sorted(facilities, key=lambda f: f.record.id):
        doc = json.loads(f.rec_bytes)
        row: dict[str, Any] = {name: f.row[name] for name in CSV_COLUMNS}
        row.update({name: dumps_compact(doc[name]).decode("utf-8") for name in JSON_COLUMNS})
        lines.append(dumps_compact(row) + b"\n")
    # read_json with fixed column types loads every row in one statement (executemany is ~1000x
    # slower). The columns struct is built from the constants above, never from record data.
    spec = "{" + ", ".join(f"{_sql_str(name)}: {_sql_str(kind)}" for name, kind in columns) + "}"
    con = connect()
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-parquet-") as tmp:
            rows_path = Path(tmp) / "rows.jsonl"
            rows_path.write_bytes(b"".join(lines))
            con.execute("SET threads = 1")  # one row group, written the same way every time
            ddl = ", ".join(f'"{name}" {kind}' for name, kind in columns)
            con.execute(f"CREATE TABLE facilities ({ddl})")
            if lines:
                con.execute(
                    "INSERT INTO facilities SELECT * FROM read_json(?, "  # noqa: S608
                    f"format = 'newline_delimited', columns = {spec})",
                    [str(rows_path)],
                )
            geo = json.dumps(geo_metadata(facilities), separators=(",", ":"), sort_keys=True)
            # COPY takes no parameters: the target path and the metadata are quoted literals.
            con.execute(
                f"COPY ({_PARQUET_SELECT}) TO {_sql_str(str(path))} (FORMAT parquet, "
                f"COMPRESSION zstd, GEOPARQUET_VERSION 'NONE', KV_METADATA {{geo: {_sql_str(geo)}}})"
            )
        result = con.execute(_PARQUET_CHECK, [str(path)]).fetchone()
    finally:
        con.close()
    if result is None:  # pragma: no cover (count(*) always returns a row)
        return 0, 0
    return int(result[0]), int(result[1])


def parquet_geo_metadata(path: Path) -> dict[str, Any]:
    """The decoded 'geo' key-value metadata of a Parquet file."""
    from atlas.geo.duck import connect

    con = connect()
    try:
        rows = con.execute(
            "SELECT decode(key), decode(value) FROM parquet_kv_metadata(?)", [str(path)]
        ).fetchall()
    finally:
        con.close()
    for key, value in rows:
        if key == "geo":
            return cast("dict[str, Any]", json.loads(value))
    raise ValueError(f"{path}: no geo metadata")


def parquet_rows(path: Path) -> tuple[list[tuple[str, str]], list[tuple[Any, ...]]]:
    """(column names and types, rows ordered by id) of a facilities.parquet, geometry as WKT."""
    from atlas.geo.duck import connect

    con = connect()
    try:
        cursor = con.execute(_PARQUET_ROWS, [str(path)])
        described = cursor.description or []
        rows = cursor.fetchall()
    finally:
        con.close()
    return [(str(d[0]), str(d[1])) for d in described], [tuple(r) for r in rows]


# ------------------------------------------------------------------------------- PMTiles


def tippecanoe_path() -> str | None:
    return shutil.which("tippecanoe")


def tippecanoe_argv() -> list[str]:
    """The command line write_pmtiles runs (tippecanoe stores it as generator_options)."""
    return ["tippecanoe", "-o", "facilities.pmtiles", *TIPPECANOE_ARGS, "facilities.geojsonl"]


def write_pmtiles(path: Path, facilities: Sequence[Facility]) -> None:
    """facilities.pmtiles from the mappable features (tippecanoe, 2.79.0 in CI).

    tippecanoe runs in a scratch directory with relative file names and argv[0] "tippecanoe", so
    the generator_options it records in the metadata do not depend on where it was run.
    """
    exe = tippecanoe_path()
    if exe is None:
        raise ReleaseError(
            ["tippecanoe is not on PATH: build 2.79.0 (docs/publishing.md) or pass --skip-pmtiles"]
        )
    mappable = [f for f in facilities if f.lonlat is not None]
    if not mappable:
        raise ReleaseError(["facilities.pmtiles: no mappable facility"])
    with tempfile.TemporaryDirectory(prefix="atlas-tippecanoe-") as tmp:
        work = Path(tmp)
        lines = [
            dumps_compact({"type": "Feature", "properties": dict(f.row), "geometry": f.geometry()})
            for f in mappable
        ]
        (work / "facilities.geojsonl").write_bytes(b"".join(line + b"\n" for line in lines))
        argv = tippecanoe_argv()
        result = subprocess.run(  # noqa: S603  (fixed argument list, no shell; exe from PATH)
            argv, executable=exe, cwd=work, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise ReleaseError([f"tippecanoe exited {result.returncode}: {result.stderr[-2000:]}"])
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(work / "facilities.pmtiles", path)


def _pmtiles_header(path: Path) -> tuple[bytes, tuple[int, ...]]:
    data = path.read_bytes()
    if len(data) < 127 or data[:7] != b"PMTiles" or data[7] != 3:
        raise ValueError(f"{path}: not a PMTiles v3 archive")
    return data, struct.unpack("<11Q", data[8:96]) + struct.unpack("<6B", data[96:102])


def pmtiles_metadata(path: Path) -> dict[str, Any]:
    """The JSON metadata of a PMTiles v3 archive as stored, generator fields included."""
    data, header = _pmtiles_header(path)
    meta_offset, meta_length, internal = header[2], header[3], header[12]
    raw = data[meta_offset : meta_offset + meta_length]
    if internal == 2:
        raw = gzip.decompress(raw)
    elif internal not in (0, 1):
        raise ValueError(f"{path}: unsupported internal compression {internal}")
    return cast("dict[str, Any]", json.loads(raw)) if raw else {}


def pmtiles_summary(path: Path) -> dict[str, Any]:
    """Tile counts, zooms and metadata of a PMTiles v3 archive (generator fields left out)."""
    _, header = _pmtiles_header(path)
    addressed, entries, contents = header[8], header[9], header[10]
    tile_compression, tile_type, min_zoom, max_zoom = header[13:17]
    metadata = pmtiles_metadata(path)
    for key in ("generator", "generator_options"):
        metadata.pop(key, None)
    return {
        "addressed_tiles": addressed,
        "tile_entries": entries,
        "tile_contents": contents,
        "tile_compression": tile_compression,
        "tile_type": tile_type,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "metadata": metadata,
    }


# ------------------------------------------------------------------------ previous release


@dataclass(frozen=True)
class PreviousRelease:
    release: str
    facilities: int
    source: str


def _counts_from_manifest(doc: Any, source: str) -> PreviousRelease:
    try:
        release = str(doc["release"])
        facilities = int(doc["counts"]["facilities"])
    except (KeyError, TypeError, ValueError) as e:
        raise ReleaseError(
            [f"previous manifest {source} has no release or counts.facilities"]
        ) from e
    return PreviousRelease(release, facilities, source)


def _fetch_json(client: httpx.Client, url: str, sleep: Callable[[float], None] | None) -> Any:
    from atlas.net import fetch

    kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    result = fetch(client, url, robots=False, min_interval=0.0, max_bytes=5_000_000, **kwargs)
    try:
        return json.loads(result.content)
    except ValueError as e:
        raise ReleaseError([f"{url} is not JSON"]) from e


def load_previous(
    spec: str,
    *,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> PreviousRelease | None:
    """The previous release's facility count. None means there is none to compare against.

    spec is "none", a URL of atlas/latest.json (a 404 means this is the first release; any other
    error fails), a manifest.json or latest.json file, or a built release directory.
    """
    if spec.strip().lower() == "none":
        return None
    if spec.startswith(("https://", "http://")):
        from atlas.net import FetchError, make_client

        client = http or make_client()
        try:
            try:
                pointer = _fetch_json(client, spec, sleep)
            except FetchError as e:
                if e.status == 404:
                    return None
                raise ReleaseError([f"previous release: {e}"]) from e
            if isinstance(pointer, dict) and "counts" in pointer:
                return _counts_from_manifest(pointer, spec)
            manifest_url = pointer.get("manifest") if isinstance(pointer, dict) else None
            if not isinstance(manifest_url, str) or not manifest_url.startswith("https://"):
                raise ReleaseError([f"previous pointer {spec} has no https manifest URL"])
            try:
                return _counts_from_manifest(_fetch_json(client, manifest_url, sleep), manifest_url)
            except FetchError as e:
                raise ReleaseError([f"previous manifest: {e}"]) from e
        finally:
            if http is None:
                client.close()
    path = Path(spec)
    if path.is_dir():
        pointer_path = path / LATEST_KEY
        if pointer_path.is_file():
            release = str(cast("dict[str, Any]", read_json(pointer_path))["release"])
            manifest_path = path / "v" / release / "manifest.json"
        else:
            found = sorted(path.glob("v/*/manifest.json"))
            if len(found) != 1:
                raise ReleaseError([f"{path}: expected one v/*/manifest.json, found {len(found)}"])
            manifest_path = found[0]
        return _counts_from_manifest(read_json(manifest_path), str(manifest_path))
    if path.is_file():
        doc = read_json(path)
        if isinstance(doc, dict) and "counts" in doc:
            return _counts_from_manifest(doc, str(path))
        if isinstance(doc, dict) and isinstance(doc.get("manifest"), str):
            return load_previous(cast("str", doc["manifest"]), http=http, sleep=sleep)
        raise ReleaseError([f"{path} is neither a manifest nor a latest.json pointer"])
    raise ReleaseError([f"previous release {spec!r}: no such URL, file or directory"])


def check_count(previous: PreviousRelease | None, facilities: int, *, allow: bool) -> str | None:
    """A problem when the facility count moved more than ±10% from the previous release."""
    if previous is None or allow:
        return None
    limit = COUNT_TOLERANCE * previous.facilities
    if abs(facilities - previous.facilities) <= limit:
        return None
    return (
        f"facilities {facilities} vs {previous.facilities} in release {previous.release}: more "
        "than ±10%; a bulk import needs the `bulk` label or --allow-count-change"
    )


# --------------------------------------------------------------------------------- build


@dataclass(frozen=True)
class BuildOptions:
    records_dir: Path
    orgs_path: Path
    out_dir: Path
    release: str
    generated_at: datetime
    layers_path: Path = DEFAULT_LAYERS
    # The release's copies of the data docs. The fixture points these (and layers_path) at frozen
    # copies under fixtures/release-inputs, so a CHANGELOG or ATTRIBUTION edit never changes it.
    attribution_path: Path = DEFAULT_ATTRIBUTION
    changelog_path: Path = DEFAULT_CHANGELOG
    previous: str = "none"
    allow_count_change: bool = False
    skip_pmtiles: bool = False
    fixture: bool = False
    tiles_base: str = DEFAULT_TILES_BASE
    git_commit: str | None = None
    pr_number: int | None = None
    schema_path: Path = Path(SCHEMA_PATH)
    today: date | None = None
    repo_root: Path = REPO_ROOT
    # Internal: `fixture --check` without tippecanoe copies the committed archive instead.
    reuse_pmtiles: Path | None = None


@dataclass
class BuildResult:
    release: str
    out_dir: Path
    manifest: dict[str, JsonValue]
    previous: PreviousRelease | None
    notes: list[str] = field(default_factory=list)

    @property
    def version_dir(self) -> Path:
        return self.out_dir / "v" / self.release


def _check_out(out: Path) -> None:
    """Refuse an --out that is a file or holds anything but a previous release build."""
    if not out.exists():
        return
    if not out.is_dir():
        raise ReleaseError([f"{out} is not a directory"])
    other = sorted(p.name for p in out.iterdir() if p.name not in RELEASE_ROOTS)
    if other:
        raise ReleaseError(
            [f"{out} holds files that are not a release ({', '.join(other)}); use another --out"]
        )


def _check_tools(opts: BuildOptions) -> None:
    """Fail before anything is written when the PMTiles step cannot run."""
    if opts.skip_pmtiles:
        return
    if opts.reuse_pmtiles is not None:
        if not opts.reuse_pmtiles.is_file():
            raise ReleaseError([f"{opts.reuse_pmtiles}: no such PMTiles archive"])
    elif tippecanoe_path() is None:
        raise ReleaseError(
            ["tippecanoe is not on PATH: build 2.79.0 (docs/publishing.md) or pass --skip-pmtiles"]
        )


def _install(staging: Path, out: Path) -> None:
    """Move a finished, checked build from staging into out, replacing the previous build.

    staging is a sibling of out, so each move is a rename on the same filesystem.
    """
    out.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_ROOTS:
        shutil.rmtree(out / name, ignore_errors=True)
        if (staging / name).exists():
            (staging / name).rename(out / name)


def _validate(opts: BuildOptions, counties: CountyIndex) -> None:
    from atlas.validate import validate_dataset

    report = validate_dataset(
        opts.records_dir,
        opts.orgs_path,
        schema_path=_resolve(opts.schema_path, opts.repo_root),
        counties=counties,
        today=opts.today or datetime.now(UTC).date(),
    )
    if not report.ok:
        shown = [f"validate: {issue.format()}" for issue in report.issues[:50]]
        if len(report.issues) > 50:
            shown.append(f"validate: ... and {len(report.issues) - 50} more issues")
        raise ReleaseError(shown)


def _check_tiles_base(base: str) -> str:
    base = base.rstrip("/")
    if not re.match(r"^https://[a-z0-9.-]+(:\d+)?$", base) and not re.match(
        r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", base
    ):
        raise ReleaseError([f"tiles base must be an https origin without a path, got {base!r}"])
    return base


def build_release(
    opts: BuildOptions,
    *,
    counties: CountyIndex,
    http: httpx.Client | None = None,
    log: Callable[[str], None] = print,
) -> BuildResult:
    """Validate, run the release checks and write the release directory (07 §6.8 steps 1-2).

    The release is built in a temporary sibling of out and moved into out only after every check
    has passed, so a failed build (tippecanoe missing or failing, a Parquet mismatch, a post-build
    check) leaves out exactly as it was. That matters for `atlas publish fixture`, whose out is
    the committed fixtures/release.
    """
    try:
        release_time(opts.release)
    except ValueError as e:
        raise ReleaseError([str(e)]) from e
    tiles_base = _check_tiles_base(opts.tiles_base)
    _check_out(opts.out_dir)
    _check_tools(opts)
    _validate(opts, counties)

    records = RecordStore(opts.records_dir).load()
    orgs = load_orgs(opts.orgs_path)
    org_kinds = {o.id: o.kind for o in orgs}
    layers = read_json(_resolve(opts.layers_path, opts.repo_root))
    layer_problems = check_layers(layers)
    if layer_problems or not isinstance(layers, dict):
        raise ReleaseError([f"{opts.layers_path}: {p}" for p in layer_problems])

    public = [public_record(r) for _, r in sorted(records.items()) if r.scope == "in_scope"]
    facilities = [make_facility(r) for r in public if r.merged_into is None]
    merged = len(public) - len(facilities)

    notes: list[str] = []
    previous_note: str | None = None
    problems: list[str] = []
    if not facilities and not opts.fixture:
        problems.append("no in-scope facility to publish")
    try:
        previous = load_previous(opts.previous, http=http)
    except ReleaseError as e:
        # The count check is skipped anyway, so a previous release that cannot be read (its
        # manifest expired by the lifecycle rule or deleted after a takedown, while
        # atlas/latest.json still points to it) must not block the recovery run.
        if not opts.allow_count_change:
            raise
        previous = None
        previous_note = (
            f"previous release not read ({'; '.join(e.problems)}); count check skipped "
            "(--allow-count-change)"
        )
    count_problem = check_count(previous, len(facilities), allow=opts.allow_count_change)
    if count_problem:
        problems.append(count_problem)
    problems += check_facilities(facilities)

    release = opts.release
    generated_at = iso_z(opts.generated_at)
    map_bytes = render_map(release, facilities, org_kinds)
    map_gzip = len(gzip_bytes(map_bytes))
    if map_gzip > MAP_BUDGET_GZIP_BYTES:
        problems.append(f"facilities-map.json: {map_gzip} bytes gzipped > {MAP_BUDGET_GZIP_BYTES}")
    if problems:
        raise ReleaseError(problems)

    root = opts.repo_root
    texts: dict[str, bytes] = {
        "facilities-map.json": map_bytes,
        "facilities.geojson": render_geojson(facilities),
        "facilities.csv": render_csv(facilities),
        "records.jsonl.gz": render_records(public),
        "summary.json": dumps_pretty(
            build_summary(release, generated_at, facilities, merged)
        ).encode("utf-8"),
        "feed.json": dumps_pretty(
            {
                "release": release,
                "generated_at": generated_at,
                "window_days": FEED_WINDOW_DAYS,
                "items": [],
            }
        ).encode("utf-8"),
        "schema/facility.v1.json": render().encode("utf-8"),
        "LICENSE-ODbL-1.0.txt": (root / "LICENSE-ODbL-1.0.txt").read_bytes(),
        "ATTRIBUTION.md": _resolve(opts.attribution_path, root).read_bytes(),
        "CHANGELOG.md": _resolve(opts.changelog_path, root).read_bytes(),
    }
    binaries = ["facilities.parquet"]
    if not opts.skip_pmtiles:
        binaries.append("facilities.pmtiles")
    names = sorted([*texts, *binaries, "README.md", "manifest.json"])
    texts["README.md"] = render_readme(release, names, fixture=opts.fixture).encode("utf-8")

    out = opts.out_dir.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-", suffix=".partial", dir=out.parent))
    try:
        manifest = _write_release(
            staging,
            opts,
            texts=texts,
            names=names,
            facilities=facilities,
            public=public,
            merged=merged,
            layers=layers,
            tiles_base=tiles_base,
            generated_at=generated_at,
            notes=notes,
        )
        post = check_release_files(staging)
        if post:
            raise ReleaseError(post)
        _install(staging, opts.out_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if previous_note is not None:
        notes.append(previous_note)
    elif previous is None:
        notes.append(f"no previous release to compare (--previous {opts.previous})")
    else:
        notes.append(
            f"previous release {previous.release}: {previous.facilities} facilities, now "
            f"{len(facilities)}"
            + (" (count check skipped: --allow-count-change)" if opts.allow_count_change else "")
        )
    for note in notes:
        log(f"note: {note}")
    return BuildResult(release, opts.out_dir, manifest, previous, notes)


def _write_release(
    out: Path,
    opts: BuildOptions,
    *,
    texts: Mapping[str, bytes],
    names: Sequence[str],
    facilities: Sequence[Facility],
    public: Sequence[FacilityRecord],
    merged: int,
    layers: dict[str, JsonValue],
    tiles_base: str,
    generated_at: str,
    notes: list[str],
) -> dict[str, JsonValue]:
    """Write every file of the release into the empty directory out; return the manifest."""
    release = opts.release
    version_dir = out / "v" / release
    for name, data in texts.items():
        target = version_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    rows, invalid = write_parquet(version_dir / "facilities.parquet", facilities)
    if rows != len(facilities) or invalid:
        raise ReleaseError(
            [f"facilities.parquet: {rows} rows for {len(facilities)} facilities, {invalid} invalid"]
        )
    if opts.skip_pmtiles:
        notes.append("facilities.pmtiles skipped (--skip-pmtiles)")
    elif opts.reuse_pmtiles is not None:
        shutil.copyfile(opts.reuse_pmtiles, version_dir / "facilities.pmtiles")
        notes.append(f"facilities.pmtiles copied from {opts.reuse_pmtiles}")
    else:
        write_pmtiles(version_dir / "facilities.pmtiles", facilities)

    for f in facilities:
        target = out / f.rec_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.rec_bytes)

    files: list[JsonValue] = []
    for name in names:
        if name == "manifest.json":
            continue
        path = version_dir / name
        data = path.read_bytes()
        files.append(
            {
                "path": name,
                "bytes": len(data),
                "sha256": sha256_bytes(data),
                "content_type": content_type_for(f"v/{release}/{name}"),
            }
        )
    by_status = dict.fromkeys(STATUSES, 0)
    by_group = dict.fromkeys(GROUPS, 0)
    for f in facilities:
        by_status[f.record.status] += 1
        by_group[status_group(f.record.status)] += 1
    manifest: dict[str, JsonValue] = {
        "release": release,
        "generated_at": generated_at,
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "license": LICENSE_ID,
        "attribution": ATTRIBUTION.format(release=release),
        "fixture": opts.fixture,
        "base_url": f"{tiles_base}/v/{release}/",
        "rec_base_url": f"{tiles_base}/rec/",
        "git": {"commit": opts.git_commit, "pr_number": opts.pr_number},
        "counts": {
            "records_public": len(public),
            "facilities": len(facilities),
            "merged": merged,
            "by_status": cast("dict[str, JsonValue]", by_status),
            "by_group": cast("dict[str, JsonValue]", by_group),
        },
        "files": files,
        "layers": layers,
    }
    (version_dir / "manifest.json").write_text(dumps_pretty(manifest), "utf-8", newline="\n")
    if not opts.fixture:
        pointer = {"release": release, "manifest": f"{tiles_base}/v/{release}/manifest.json"}
        target = out / LATEST_KEY
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dumps_pretty(pointer), "utf-8", newline="\n")
    return manifest


LAYER_KEYS = frozenset({"url", "asOf", "label", "attribution", "frozen"})
# A metric overlay's JSON lives on the site, not on R2, so its entry gives the site path (07 §10.3).
SITE_DATA_PATH_RE = re.compile(r"^/atlas/data/[a-z0-9-]+\.json$")


def check_layers(layers: object) -> list[str]:
    """overlays/out/layers.json: {name: {url, asOf, label, attribution, frozen, ...}} (07 §11.1).

    The five keys are required; other keys are allowed, since contract 1 grows only by additions
    (07 §11.3). url is an https URL (R2 overlays, the basemap) or a site path
    /atlas/data/{layer}.json (metric JSON, 07 §10.3).
    """
    if not isinstance(layers, dict) or not layers:
        return ["must be a non-empty JSON object of layers"]
    problems: list[str] = []
    for name, layer in sorted(layers.items()):
        if not isinstance(layer, dict) or not LAYER_KEYS.issubset(layer):
            problems.append(f"layer {name!r} must have {', '.join(sorted(LAYER_KEYS))}")
            continue
        url = str(layer["url"])
        if not url.startswith("https://") and not SITE_DATA_PATH_RE.match(url):
            problems.append(f"layer {name!r}: url must be https or /atlas/data/{{layer}}.json")
        if not isinstance(layer["frozen"], bool):
            problems.append(f"layer {name!r}: frozen must be true or false")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(layer["asOf"])):
            problems.append(f"layer {name!r}: asOf must be YYYY-MM-DD")
    return problems


def check_facilities(facilities: Sequence[Facility]) -> list[str]:
    """Every point inside lat [18, 72] and lon [-180, -64]; every rec/ object within budget."""
    problems: list[str] = []
    for f in facilities:
        if f.lonlat is not None:
            lon, lat = f.lonlat
            if not (
                LAT_BOUNDS[0] <= lat <= LAT_BOUNDS[1] and LON_BOUNDS[0] <= lon <= LON_BOUNDS[1]
            ):
                problems.append(f"{f.record.id}: point ({lat}, {lon}) is outside the US bounds")
        if len(f.rec_bytes) > REC_MAX_BYTES:
            problems.append(f"{f.rec_key}: {len(f.rec_bytes)} bytes > {REC_MAX_BYTES}")
    return problems


def check_release_files(out: Path) -> list[str]:
    """The upload allow-list, the key prefixes and the per-file size cap over a release dir."""
    problems: list[str] = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        key = path.relative_to(out).as_posix()
        try:
            content_type_for(key)
            cache_control_for(key)
        except DisallowedKey as e:
            problems.append(str(e))
        size = path.stat().st_size
        if size > FILE_MAX_BYTES:
            problems.append(f"{key}: {size} bytes > {FILE_MAX_BYTES}")
    return problems


# --------------------------------------------------------------------------------- verify


def verify_release(release_dir: Path, release: str | None = None) -> tuple[str | None, list[str]]:
    """Re-check a built release: manifest checksums and bytes, allow-list, key prefixes, rec/.

    Returns the release id (found or given) and the problems, empty when the directory is good.
    """
    problems: list[str] = []
    if not release_dir.is_dir():
        return release, [f"{release_dir} is not a directory"]
    versions = (
        sorted(p.name for p in (release_dir / "v").iterdir())
        if (release_dir / "v").is_dir()
        else []
    )
    if release is None:
        if len(versions) != 1:
            return None, [f"{release_dir}/v holds {len(versions)} releases; pass --release"]
        release = versions[0]
    if not RELEASE_RE.match(release):
        return release, [f"release id must be YYYYMMDD-HHMM, got {release!r}"]
    if versions != [release]:
        problems.append(f"{release_dir}/v must hold only {release}, found {', '.join(versions)}")
    for entry in sorted(release_dir.iterdir()):
        if entry.name not in ("v", "rec", "atlas"):
            problems.append(f"unexpected {entry.name} in {release_dir}")
    problems += check_release_files(release_dir)

    version_dir = release_dir / "v" / release
    manifest_path = version_dir / "manifest.json"
    if not manifest_path.is_file():
        return release, [*problems, f"{manifest_path} not found"]
    try:
        manifest = cast("dict[str, Any]", read_json(manifest_path))
        listed = {str(f["path"]): f for f in manifest["files"]}
    except (ValueError, KeyError, TypeError) as e:
        return release, [*problems, f"{manifest_path}: unreadable manifest ({e})"]
    if manifest.get("release") != release:
        problems.append(f"manifest release {manifest.get('release')!r} is not {release}")
    if manifest.get("contract_version") != CONTRACT_VERSION:
        problems.append(f"manifest contract_version is not {CONTRACT_VERSION}")
    paths = [str(f["path"]) for f in manifest["files"]]
    if paths != sorted(paths):
        problems.append("manifest files are not sorted by path")
    actual = {
        p.relative_to(version_dir).as_posix()
        for p in version_dir.rglob("*")
        if p.is_file() and p != manifest_path
    }
    for name in sorted(actual - set(listed)):
        problems.append(f"v/{release}/{name} is not listed in the manifest")
    for name in sorted(set(listed) - actual):
        problems.append(f"v/{release}/{name} is listed in the manifest but missing")
    for name in sorted(actual & set(listed)):
        entry = listed[name]
        data = (version_dir / name).read_bytes()
        if entry.get("bytes") != len(data):
            problems.append(
                f"v/{release}/{name}: {len(data)} bytes, manifest says {entry.get('bytes')}"
            )
        if entry.get("sha256") != sha256_bytes(data):
            problems.append(f"v/{release}/{name}: SHA-256 does not match the manifest")
        try:
            expected_type = content_type_for(f"v/{release}/{name}")
        except DisallowedKey:
            continue  # reported by check_release_files
        if entry.get("content_type") != expected_type:
            problems.append(f"v/{release}/{name}: content_type is not {expected_type}")

    rec_keys: set[str] = set()
    for path in sorted(p for p in (release_dir / "rec").rglob("*") if p.is_file()):
        key = path.relative_to(release_dir).as_posix()
        m = _REC_KEY_RE.match(key)
        if not m:
            problems.append(f"{key} is not rec/{{id}}/{{sha256[:12]}}.json")
            continue
        data = path.read_bytes()
        if sha256_bytes(data)[:12] != m.group(2):
            problems.append(f"{key}: name is not the SHA-256 prefix of its bytes")
        if len(data) > REC_MAX_BYTES:
            problems.append(f"{key}: {len(data)} bytes > {REC_MAX_BYTES}")
        try:
            if json.loads(data).get("id") != m.group(1):
                problems.append(f"{key}: record id does not match the key")
        except ValueError:
            problems.append(f"{key}: not JSON")
        rec_keys.add(key)
    map_path = version_dir / "facilities-map.json"
    if map_path.is_file():
        try:
            features = cast("dict[str, Any]", read_json(map_path))["features"]
            map_keys = {
                f"rec/{f['properties']['id']}/{f['properties']['h']}.json" for f in features
            }
        except (ValueError, KeyError, TypeError) as e:
            problems.append(f"facilities-map.json: unreadable ({e})")
        else:
            for key in sorted(map_keys - rec_keys):
                problems.append(f"facilities-map.json: {key} is missing")
            for key in sorted(rec_keys - map_keys):
                problems.append(f"{key} is not a facility in facilities-map.json")

    atlas_dir = release_dir / "atlas"
    for path in sorted(p for p in atlas_dir.rglob("*") if p.is_file()):
        key = path.relative_to(release_dir).as_posix()
        if key != LATEST_KEY:
            problems.append(f"{key}: a release build writes only {LATEST_KEY} under atlas/")
            continue
        if manifest.get("fixture"):
            problems.append(f"{LATEST_KEY} must not exist for the fixture release")
        try:
            pointer = cast("dict[str, Any]", read_json(path))
        except ValueError:
            problems.append(f"{LATEST_KEY}: not JSON")
            continue
        if pointer.get("release") != release:
            problems.append(f"{LATEST_KEY} points to {pointer.get('release')!r}, not {release}")
        manifest_url = str(pointer.get("manifest", ""))
        if not manifest_url.endswith(f"/v/{release}/manifest.json"):
            problems.append(
                f"{LATEST_KEY}: manifest URL does not end in /v/{release}/manifest.json"
            )
    return release, problems


# --------------------------------------------------------------------------------- upload


def upload_release(
    release_dir: Path,
    release: str,
    uploader: Uploader | None,
    *,
    latest: bool = True,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> UploadReport:
    """Verify, then upload in order: v/{release}/**, its manifest, rec/**, atlas/latest.json.

    uploader may be None only for a dry run.
    """
    _, problems = verify_release(release_dir, release)
    if problems:
        raise ReleaseError(problems)
    manifest = cast("dict[str, Any]", read_json(release_dir / "v" / release / "manifest.json"))
    if latest and manifest.get("fixture"):
        raise ReleaseError(["the fixture release never writes atlas/latest.json; pass --no-latest"])
    items = plan_release(release_dir, release, latest=latest)
    report = UploadReport()
    if dry_run:
        for item in items:
            log(f"would put {item.key} ({item.content_type}; {item.cache_control})")
        return report
    if uploader is None:
        raise ValueError("an uploader is needed unless dry_run is set")
    for item in items:
        upload_item(uploader, item, report=report, log=log)
    log(f"upload {release}: {report.summary()}")
    return report


# ------------------------------------------------------------------------------- takedown

_RECORD_ID_RE = re.compile(r"^gwa-[0-9a-hjkmnp-tv-z]{26}$")


@dataclass
class TakedownPlan:
    """What `atlas publish takedown` deletes from atlas-tiles (07 §5.4, docs/publishing.md §7)."""

    ids: list[str]
    keep: list[str]
    releases: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _release_ids(bucket: Bucket) -> dict[str, list[str]]:
    """{release: its keys} for every v/{release}/ in the bucket."""
    found: dict[str, list[str]] = {}
    for key in bucket.list_keys("v/"):
        release = key.split("/")[1]
        if RELEASE_RE.match(release):
            found.setdefault(release, []).append(key)
    return found


def _map_hashes(bucket: Bucket, release: str, ids: set[str]) -> dict[str, str]:
    """{id: h} of the ids that are facilities in a release's facilities-map.json."""
    data = bucket.get(f"v/{release}/facilities-map.json")
    if data is None:
        return {}
    try:
        features = json.loads(data)["features"]
        pairs = ((str(f["properties"]["id"]), str(f["properties"]["h"])) for f in features)
        return {rid: h for rid, h in pairs if rid in ids}
    except (ValueError, KeyError, TypeError):
        return {}


def _holds_ids(bucket: Bucket, release: str, ids: set[str]) -> bool | None:
    """Whether a release's records.jsonl.gz holds one of ids; None when it cannot be read."""
    data = bucket.get(f"v/{release}/records.jsonl.gz")
    if data is None:
        return None
    try:
        lines = gzip.decompress(data).decode("utf-8").splitlines()
        return any(json.loads(line).get("id") in ids for line in lines if line.strip())
    except (OSError, EOFError, ValueError, AttributeError):
        return None


def plan_takedown(bucket: Bucket, ids: Sequence[str], *, keep: Sequence[str] = ()) -> TakedownPlan:
    """The objects to delete so that ids are served from no release and no rec/ object.

    Kept: the release atlas/latest.json points to (the hotfix release, published first), every
    release in keep, and the fixture release (invented test data). Every other release whose
    records.jsonl.gz holds one of ids, or cannot be read (a partial upload), is deleted in full,
    its manifest first. rec/{id}/* is deleted except the objects a kept release still maps.
    Raises ReleaseError when an id or release id is malformed, nothing says which release to
    keep, or a kept release still maps the same rec/ object as a release being deleted (the
    hotfix has not been published yet).
    """
    problems = [
        f"{rid!r} is not a record id (gwa-...)" for rid in ids if not _RECORD_ID_RE.match(rid)
    ]
    problems += [
        f"--keep {r!r} is not a release id (YYYYMMDD-HHMM)" for r in keep if not RELEASE_RE.match(r)
    ]
    if not ids:
        problems.append("no record id given")
    if problems:
        raise ReleaseError(problems)
    wanted = set(ids)
    releases = _release_ids(bucket)
    kept = set(keep)
    latest = bucket.get(LATEST_KEY)
    if latest is not None:
        try:
            kept.add(str(json.loads(latest)["release"]))
        except (ValueError, KeyError, TypeError) as e:
            raise ReleaseError([f"{LATEST_KEY} is unreadable ({e}); pass --keep RELEASE"]) from e
    elif not kept:
        raise ReleaseError([f"{LATEST_KEY} not found; pass --keep with the hotfix release"])
    missing = sorted(r for r in kept if f"v/{r}/manifest.json" not in releases.get(r, []))
    if missing:
        raise ReleaseError([f"kept release {r} has no v/{r}/manifest.json" for r in missing])
    kept.add(FIXTURE_RELEASE)

    plan = TakedownPlan(ids=sorted(wanted), keep=sorted(kept & set(releases)))
    kept_rec: dict[str, str] = {}
    for release in sorted(kept & set(releases)):
        for rid, h in _map_hashes(bucket, release, wanted).items():
            kept_rec[f"rec/{rid}/{h}.json"] = release
        if release != FIXTURE_RELEASE and _holds_ids(bucket, release, wanted):
            plan.notes.append(
                f"kept release {release} still holds a taken-down id: check that the hotfix "
                "redacted the record rather than leaving it as it was"
            )
    stale: list[str] = []
    for release, keys in sorted(releases.items()):
        if release in kept:
            continue
        holds = _holds_ids(bucket, release, wanted)
        if holds is False:
            continue
        if holds is None:
            plan.notes.append(f"v/{release}/records.jsonl.gz unreadable or missing: deleted too")
        hashes = _map_hashes(bucket, release, wanted)
        for rid, h in sorted(hashes.items()):
            by = kept_rec.get(f"rec/{rid}/{h}.json")
            if by is not None and by != FIXTURE_RELEASE:  # fixture records are invented
                stale.append(
                    f"rec/{rid}/{h}.json is mapped by kept release {by} and by {release}: the "
                    "record is unchanged; land the hotfix PR and publish before the takedown"
                )
        manifest = f"v/{release}/manifest.json"
        plan.releases.append(release)
        plan.delete += [k for k in keys if k == manifest] + [k for k in keys if k != manifest]
    if stale:
        raise ReleaseError(stale)
    for rid in plan.ids:
        plan.delete += [k for k in bucket.list_keys(f"rec/{rid}/") if k not in kept_rec]
    return plan


def run_takedown(
    bucket: Bucket,
    plan: TakedownPlan,
    *,
    tiles_base: str = DEFAULT_TILES_BASE,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Delete the plan's keys (or list them with dry_run); return their public URLs to purge."""
    base = _check_tiles_base(tiles_base)
    for note in plan.notes:
        log(f"note: {note}")
    log(f"keep: {', '.join(plan.keep) or 'none'}")
    for key in plan.delete:
        if dry_run:
            log(f"would delete {key}")
        else:
            bucket.delete(key)
            log(f"deleted {key}")
    urls = [f"{base}/{key}" for key in plan.delete]
    verb = "would delete" if dry_run else "deleted"
    log(
        f"takedown {', '.join(plan.ids)}: {verb} {len(plan.delete)} objects in "
        f"{len(plan.releases)} releases and rec/"
    )
    if urls and not dry_run:
        log(
            "next: purge the edge cache (Caching > Configuration > Purge Cache > Custom Purge > "
            f"Hostname {base.removeprefix('https://')}, or these URLs)"
        )
    return urls


# -------------------------------------------------------------------------------- fixture


def fixture_options(out_dir: Path, *, today: date | None = None) -> BuildOptions:
    """`atlas publish build --records tests/fixtures/records --orgs tests/fixtures/orgs.json
    --layers fixtures/release-inputs/layers.json --attribution fixtures/release-inputs/ATTRIBUTION.md
    --changelog fixtures/release-inputs/CHANGELOG.md --release 20000101-0000 --fixture
    --deterministic --previous none --out fixtures/release`.

    The fixture reads frozen copies of layers.json, ATTRIBUTION.md and CHANGELOG.md, never the
    living repo files, so a changelog entry, a new source or the weekly overlays PR cannot make
    `fixture --check` (a required CI check) fail. Only the records, the code, the schema and the
    ODbL text (pinned by tests/test_repo_files.py) feed it from the repo.
    """
    root = REPO_ROOT
    inputs = root / FIXTURE_INPUTS
    return BuildOptions(
        records_dir=root / FIXTURE_RECORDS,
        orgs_path=root / FIXTURE_ORGS,
        layers_path=inputs / "layers.json",
        attribution_path=inputs / "ATTRIBUTION.md",
        changelog_path=inputs / "CHANGELOG.md",
        out_dir=out_dir,
        release=FIXTURE_RELEASE,
        generated_at=release_time(FIXTURE_RELEASE),
        previous="none",
        fixture=True,
        tiles_base=DEFAULT_TILES_BASE,
        git_commit="fixture",
        pr_number=None,
        schema_path=root / SCHEMA_PATH,
        today=today,
        repo_root=root,
    )


def _normalized_manifest(path: Path) -> dict[str, Any]:
    doc = cast("dict[str, Any]", read_json(path))
    for entry in doc.get("files", []):
        if str(entry.get("path", "")).endswith(BINARY_COMPARED):
            entry.pop("bytes", None)
            entry.pop("sha256", None)
    return doc


def compare_releases(expected: Path, actual: Path, *, compare_pmtiles: bool) -> list[str]:
    """Differences between a committed release dir and a rebuild (07 §11.3).

    Text files and .jsonl.gz must be byte-identical. facilities.parquet is compared by rows and
    'geo' metadata through DuckDB; facilities.pmtiles by tile counts and metadata, and only when
    compare_pmtiles is set. In manifest.json those two files' bytes and SHA-256 are not compared,
    since a DuckDB or tippecanoe upgrade may re-encode the same content.
    """
    diffs: list[str] = []

    def listing(root: Path) -> set[str]:
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}

    want, got = listing(expected), listing(actual)
    for name in sorted(want - got):
        diffs.append(f"{name}: missing from the rebuild")
    for name in sorted(got - want):
        diffs.append(f"{name}: not in the committed fixture")
    for name in sorted(want & got):
        a, b = expected / name, actual / name
        if name.endswith("/manifest.json"):
            if _normalized_manifest(a) != _normalized_manifest(b):
                diffs.append(f"{name}: differs")
        elif name.endswith(".parquet"):
            if parquet_rows(a) != parquet_rows(b):
                diffs.append(f"{name}: rows differ")
            if parquet_geo_metadata(a) != parquet_geo_metadata(b):
                diffs.append(f"{name}: geo metadata differs")
        elif name.endswith(".pmtiles"):
            if compare_pmtiles and pmtiles_summary(a) != pmtiles_summary(b):
                diffs.append(f"{name}: tile counts or metadata differ")
        elif a.read_bytes() != b.read_bytes():
            diffs.append(f"{name}: differs")
    return diffs


def check_fixture(
    committed: Path, *, counties: CountyIndex, today: date | None = None
) -> tuple[list[str], list[str]]:
    """Rebuild the fixture in a temp dir and compare it with committed. Returns (diffs, notes)."""
    _, problems = verify_release(committed, FIXTURE_RELEASE)
    if problems:
        return [f"committed fixture: {p}" for p in problems], []
    notes: list[str] = []
    committed_pmtiles = committed / "v" / FIXTURE_RELEASE / "facilities.pmtiles"
    has_tippecanoe = tippecanoe_path() is not None
    with tempfile.TemporaryDirectory(prefix="atlas-fixture-") as tmp:
        out = Path(tmp) / "release"
        opts = fixture_options(out, today=today)
        if not has_tippecanoe:
            if committed_pmtiles.exists():
                opts = replace(opts, reuse_pmtiles=committed_pmtiles)
            else:
                opts = replace(opts, skip_pmtiles=True)
            notes.append(
                "tippecanoe is not on PATH: facilities.pmtiles was not rebuilt or compared"
            )
        build_release(opts, counties=counties, log=lambda _: None)
        diffs = compare_releases(committed, out, compare_pmtiles=has_tippecanoe)
    return diffs, notes
