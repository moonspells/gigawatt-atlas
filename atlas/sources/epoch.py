"""Epoch AI Frontier Data Centers: a pipeline seed (07 §4.1, §4.3).

Reads https://epoch.ai/data/data_centers/data_centers.zip (CC BY 4.0): README.md (the license is
checked), data_centers.csv (one row per site) and data_center_timelines.csv (dated rows per site).
Only US sites are imported. Each becomes a `project` (a `campus` once a building is operational)
whose status_history comes from the timeline through the status crosswalk (07 §4.7):

- rows are mapped with atlas.crosswalk.from_epoch_row and an event is kept only where the status
  changes;
- rows dated after today are Epoch's projections and become planned events, which never set the
  status (07 §2.3);
- capacity and water use come from the latest row dated today or earlier.

The CSV has an address but no coordinates. A cited entry in config/overrides/epoch.json wins;
otherwise the address goes through atlas.geocode (Census Geocoder, Gazetteer place, county by
name). Sites with no address and no override become `missing_location` review items, and sites the
chain cannot place become `geocode_failed` items; neither becomes a record.

Importing this module does no I/O.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, TypeAdapter, ValidationError

from atlas.crosswalk import from_epoch_row
from atlas.schema.record import PLACEHOLDER_ID, AtlasModel, FacilityRecord, Precision, SourceType
from atlas.schema.rollup import RollupError, apply_rollup
from atlas.sources.base import Candidate, ImportResult, ReviewItem, load_input
from atlas.text import find_personal_data, strip_invisible

if TYPE_CHECKING:
    from atlas.geo.counties import CountyIndex
    from atlas.geocode import CensusGeocoder, Gazetteer, GeocodeResult
    from atlas.net import FetchError, FetchResult
    from atlas.sources.base import ImportContext

ZIP_URL = "https://epoch.ai/data/data_centers/data_centers.zip"
DATASET_URL = "https://epoch.ai/data/ai-data-centers"
LICENSE = "CC-BY-4.0"
LICENSE_MARKER = "Creative Commons Attribution"
OVERRIDES_PATH = Path("config/overrides/epoch.json")
COUNTRY = "United States"
MAX_LINK_SOURCES = 5
NOTE_MAX = 200
CONFIDENCE = 0.70

SITE_COLUMNS = (
    "Name",
    "Current total capital cost (2025 USD billions)",
    "Owner",
    "Users",
    "Selected Sources",
    "Project",
    "Country",
    "Address",
)
TIMELINE_COLUMNS = (
    "Data center",
    "Date",
    "Construction status",
    "Buildings operational",
    "IT power (MW)",
    "Power (MW)",
    "Water use (MGD)",
)
S1_SUPPORTS = (
    "/canonical_name",
    "/aliases",
    "/parties",
    "/location",
    "/capacity",
    "/cooling",
    "/money",
    "/status_history",
)

_TAG_RE = re.compile(r"#(\w+)")
_DROPPED_TAGS = frozenset({"speculative", "unlikely"})
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_SPACE_RE = re.compile(r"\s+")


def epoch_error(message: str) -> FetchError:
    """The Epoch download or the overrides file cannot be used (license, layout or content).

    A FetchError, so `atlas import` reports it and exits 1; atlas.net (httpx) loads only here.
    """
    from atlas.net import FetchError

    return FetchError(message)


class LocationOverride(AtlasModel):
    """One entry of config/overrides/epoch.json: a location taken from a cited source."""

    state_abbr: str = Field(pattern=r"^[A-Z]{2}$")
    county_fips: str | None = Field(None, pattern=r"^\d{5}$")
    city: str | None = None
    lat: float | None = Field(None, ge=18, le=72)
    lon: float | None = Field(None, ge=-180, le=-64)
    precision: Precision
    source_url: HttpUrl
    note: str = Field(min_length=1, max_length=300)
    publisher: str | None = None
    source_type: SourceType | None = None


_OVERRIDES = TypeAdapter(dict[str, LocationOverride])


@dataclass(frozen=True)
class Site:
    """The fields of one data_centers.csv row that the importer uses."""

    name: str
    address: str
    owner: str
    users: str
    project: str
    selected_sources: str
    capital_cost_billions: float | None


@dataclass(frozen=True)
class TimelineRow:
    day: date
    status_text: str
    buildings_operational: float | None
    it_mw: float | None
    power_mw: float | None
    water_mgd: float | None


@dataclass(frozen=True)
class Placed:
    """A resolved location plus the source that supports it (None: the Epoch dataset)."""

    location: dict[str, Any]
    city_or_county: str | None  # for the canonical name: the city, else the county's full name
    confidence: float
    method: str
    override: LocationOverride | None


# ---------------------------------------------------------------------------- small parsers


def clean_text(s: str) -> str:
    return _SPACE_RE.sub(" ", strip_invisible(s)).strip()


def parse_number(s: str | None) -> float | None:
    """A float, or None for an empty or unreadable cell."""
    if s is None or not s.strip():
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def positive(v: float | None) -> float | None:
    return v if v is not None and v > 0 else None


def tagged_names(text: str) -> list[str]:
    """Comma-separated names with confidence tags: keep #confident, #likely and untagged names,
    drop #speculative and #unlikely, strip the tags, de-duplicate."""
    out: list[str] = []
    seen: set[str] = set()
    for part in strip_invisible(text).split(","):
        tags = {t.casefold() for t in _TAG_RE.findall(part)}
        name = clean_text(_TAG_RE.sub("", part))
        if not name or tags & _DROPPED_TAGS:
            continue
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def strip_links(text: str) -> str:
    """Markdown links reduced to their text."""
    return _MD_LINK_RE.sub(lambda m: m.group(1), text)


def event_note(text: str) -> str | None:
    """The construction-status text as an event note: links reduced, at most 200 characters,
    dropped if it looks like it holds contact details."""
    note = clean_text(strip_links(text))
    if not note or find_personal_data(note):
        return None
    if len(note) > NOTE_MAX:
        note = note[: NOTE_MAX - 1].rstrip() + "…"
    return note


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def link_source_type(url: str) -> SourceType:
    """sec.gov is an SEC filing, other .gov and .us hosts are government records, else news."""
    host = host_of(url)
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return "sec_filing"
    if host.endswith((".gov", ".us")):
        return "government_record"
    return "news"


def selected_links(markdown: str) -> list[tuple[str, str]]:
    """(text, url) of the "- [text](url)" links, valid URLs only, de-duplicated, in order."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _MD_LINK_RE.finditer(strip_invisible(markdown)):
        title, url = clean_text(m.group(1)), m.group(2).strip()
        try:
            normalized = str(HttpUrl(url))
        except ValidationError:
            continue
        if normalized in seen or find_personal_data(title):
            continue
        seen.add(normalized)
        out.append((title, url))
    return out


# ---------------------------------------------------------------------------- reading the ZIP


def _rows(text: str, required: Sequence[str], member: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        raise epoch_error(f"Epoch {member} lacks columns {missing}; the layout changed")
    return [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]


def read_zip(path: Path) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """README.md, data_centers.csv rows and data_center_timelines.csv rows, read with limits."""
    from atlas.safezip import UnsafeZipError, open_zip

    try:
        with open_zip(path, max_members=20, max_member_bytes=20_000_000) as zf:
            names = set(zf.names())
            for member in ("README.md", "data_centers.csv", "data_center_timelines.csv"):
                if member not in names:
                    raise epoch_error(f"Epoch ZIP lacks {member}")
            readme = zf.read("README.md").decode("utf-8-sig")
            sites = zf.read("data_centers.csv").decode("utf-8-sig")
            timelines = zf.read("data_center_timelines.csv").decode("utf-8-sig")
    except UnsafeZipError as e:
        raise epoch_error(f"Epoch ZIP refused: {e}") from e
    if LICENSE_MARKER not in readme:
        raise epoch_error(
            f"Epoch README.md no longer says {LICENSE_MARKER!r}; check the license before importing"
        )
    return (
        readme,
        _rows(sites, SITE_COLUMNS, "data_centers.csv"),
        _rows(timelines, TIMELINE_COLUMNS, "data_center_timelines.csv"),
    )


def load_overrides(path: Path) -> dict[str, LocationOverride]:
    from atlas.geo.counties import resolve_reference

    path = resolve_reference(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise epoch_error(f"cannot read the overrides file {path}: {e}") from e
    try:
        return _OVERRIDES.validate_json(text)
    except ValidationError as e:
        raise epoch_error(f"{path} is not a valid overrides file: {e}") from e


def timeline_rows(rows: list[dict[str, str]]) -> dict[str, list[TimelineRow]]:
    """Timeline rows by site name, sorted by date; rows without a readable date are skipped."""
    out: dict[str, list[TimelineRow]] = {}
    for row in rows:
        try:
            day = date.fromisoformat(row["Date"].strip())
        except ValueError:
            continue
        out.setdefault(clean_text(row["Data center"]), []).append(
            TimelineRow(
                day=day,
                status_text=row["Construction status"],
                buildings_operational=parse_number(row["Buildings operational"]),
                it_mw=parse_number(row["IT power (MW)"]),
                power_mw=parse_number(row["Power (MW)"]),
                water_mgd=parse_number(row["Water use (MGD)"]),
            )
        )
    for site_rows in out.values():
        site_rows.sort(key=lambda r: r.day)
    return out


# ---------------------------------------------------------------------------- mapping


def status_events(rows: Sequence[TimelineRow], today: date) -> list[dict[str, Any]]:
    """One event per status change; rows after today are planned (07 §2.3)."""
    events: list[dict[str, Any]] = []
    previous: str | None = None
    for row in rows:
        cw = from_epoch_row(row.status_text, row.buildings_operational)
        if cw.status == previous:
            continue
        previous = cw.status
        event: dict[str, Any] = {
            "seq": len(events) + 1,
            "status": cw.status,
            "event": cw.event,
            "as_of": {"value": row.day.isoformat(), "precision": "day"},
            "planned": row.day > today,
            "source_ids": ["s1"],
            "note": event_note(cw.label),
        }
        events.append(event)
    return events


def _location_from_result(r: GeocodeResult) -> dict[str, Any]:
    return {
        "lat": r.lat,
        "lon": r.lon,
        "precision": r.precision,
        "street": r.street,
        "city": r.city,
        "postcode": r.postcode,
        "county_name": r.county_name,
        "county_fips": r.county_fips,
        "state_abbr": r.state_abbr,
        "geocode_method": r.method,
    }


def place_override(
    name: str, ov: LocationOverride, *, counties: CountyIndex, gazetteer: Gazetteer
) -> Placed:
    """The location an override describes, checked like `atlas validate` rule 5 would."""
    from atlas.geocode import COUNTY_CHECKED_PRECISIONS, POINT_IN_POLYGON_PRECISIONS

    def bad(why: str) -> FetchError:
        return epoch_error(f"config/overrides/epoch.json entry {name!r}: {why}")

    county = counties.get(ov.county_fips) if ov.county_fips else None
    if ov.county_fips and (county is None or county.state_abbr != ov.state_abbr):
        raise bad(f"county_fips {ov.county_fips} is not a county of {ov.state_abbr}")
    lat, lon = ov.lat, ov.lon
    if (lat is None) != (lon is None):
        raise bad("set both lat and lon, or neither")
    method: str | None = "manual"
    if lat is None or lon is None:
        if ov.precision == "county" and county is not None:
            lat, lon = counties.centroid(county.fips)
            method = "county_centroid"
        elif ov.precision == "locality" and ov.city:
            hit = gazetteer.place(ov.state_abbr, ov.city)
            if hit is None:
                raise bad(f"no Gazetteer place {ov.city!r} in {ov.state_abbr}")
            lat, lon = hit[0], hit[1]
            method = "gazetteer"
        elif ov.precision in ("state", "unknown"):
            method = None
        elif ov.precision == "county":
            raise bad("precision county needs county_fips (or lat and lon)")
        elif ov.precision == "locality":
            raise bad("precision locality needs city (or lat and lon)")
        else:
            raise bad(f"precision {ov.precision} needs lat and lon")
    elif ov.precision in POINT_IN_POLYGON_PRECISIONS and county is None:
        county = counties.lookup(lat, lon)
    if lat is not None and lon is not None:
        if ov.precision in COUNTY_CHECKED_PRECISIONS:
            if county is None or not counties.contains(county.fips, lat, lon):
                raise bad(f"({lat}, {lon}) is not inside the county")
        elif not counties.in_state(ov.state_abbr, lat, lon):
            raise bad(f"({lat}, {lon}) is not inside {ov.state_abbr}")
    location = {
        "lat": lat,
        "lon": lon,
        "precision": ov.precision,
        "city": ov.city,
        "county_name": county.name if county else None,
        "county_fips": county.fips if county else None,
        "state_abbr": ov.state_abbr,
        "geocode_method": method,
    }
    where = ov.city or (gazetteer.county_full_name(county.fips) if county else None)
    return Placed(location, where, 0.85, "override", ov)


def site_record(
    site: Site,
    rows: Sequence[TimelineRow],
    placed: Placed,
    *,
    ctx: ImportContext,
    retrieved_at: str,
) -> FacilityRecord:
    """The candidate record for one site (id PLACEHOLDER_ID, rollup applied)."""
    actual = [r for r in rows if r.day <= ctx.today]
    latest = actual[-1] if actual else None
    events = status_events(rows, ctx.today)
    operational = any((r.buildings_operational or 0) > 0 for r in actual)

    sources: list[dict[str, Any]] = [
        {
            "id": "s1",
            "url": DATASET_URL,
            "publisher": "Epoch AI",
            "title": "AI data centers",
            "source_type": "open_dataset",
            "license": LICENSE,
            "retrieved_at": retrieved_at,
            "supports": [
                s for s in S1_SUPPORTS if not (s == "/location" and placed.override is not None)
            ],
        }
    ]
    location_sid = "s1"
    if placed.override is not None:
        ov = placed.override
        location_sid = "s2"
        sources.append(
            {
                "id": location_sid,
                "url": str(ov.source_url),
                "publisher": ov.publisher or host_of(str(ov.source_url)),
                "title": None,
                "source_type": ov.source_type or link_source_type(str(ov.source_url)),
                "retrieved_at": retrieved_at,
                "supports": ["/location"],
            }
        )
    cited = {str(HttpUrl(str(s["url"]))) for s in sources}
    links = [
        (t, u) for t, u in selected_links(site.selected_sources) if str(HttpUrl(u)) not in cited
    ]
    for title, url in links[:MAX_LINK_SOURCES]:
        sources.append(
            {
                "id": f"s{len(sources) + 1}",
                "url": url,
                "publisher": host_of(url),
                "title": title or None,
                "source_type": link_source_type(url),
                "retrieved_at": retrieved_at,
                "supports": [],
            }
        )

    imported = {"confidence": CONFIDENCE, "method": "imported", "source_ids": ["s1"]}
    field_meta: dict[str, Any] = {
        "/location": {
            "confidence": placed.confidence,
            "method": "stated" if placed.override is not None else "derived",
            "source_ids": [location_sid],
        }
    }
    capacity: dict[str, Any] = {}
    cooling: dict[str, Any] = {}
    if latest is not None:
        it_mw, facility_mw = positive(latest.it_mw), positive(latest.power_mw)
        if it_mw is not None:
            capacity["it_mw"] = it_mw
            field_meta["/capacity/it_mw"] = imported
        if facility_mw is not None:
            capacity["facility_mw"] = facility_mw
            field_meta["/capacity/facility_mw"] = imported
        water = positive(latest.water_mgd)
        if water is not None:
            cooling["water_use_mgd"] = water
            field_meta["/cooling/water_use_mgd"] = imported
    money: dict[str, Any] = {}
    cost = positive(site.capital_cost_billions)
    if cost is not None:
        money = {
            "investment_usd": float(round(cost * 1e9)),
            "investment_basis": "estimate",
            "currency_year": 2025,
        }
        field_meta["/money/investment_usd"] = imported

    ref = {"source_ids": ["s1"]}
    parties = {
        "operator": [{"name": n, **ref} for n in tagged_names(site.owner)],
        "tenant": [{"name": n, **ref} for n in tagged_names(site.users)],
    }
    aliases = [{"name": n, "kind": "codename", **ref} for n in tagged_names(site.project)]
    where = placed.city_or_county
    st = placed.location["state_abbr"]
    canonical = f"{site.name} ({where}, {st})" if where else f"{site.name} ({st})"
    ts = ctx.now.isoformat()
    doc: dict[str, Any] = {
        "id": PLACEHOLDER_ID,
        "record_type": "campus" if operational else "project",
        "canonical_name": canonical,
        "aliases": aliases,
        "parties": parties,
        "purpose": "unknown",
        "status": events[0]["status"],
        "evidence_level": "reported",
        "status_history": events,
        "location": placed.location,
        "capacity": capacity,
        "cooling": cooling,
        "money": money,
        "external_ids": {"epoch_name": [site.name]},
        "sources": sources,
        "field_meta": field_meta,
        "review": {"state": "machine"},
        "created_at": ts,
        "updated_at": ts,
        "last_verified_at": ts,
    }
    return apply_rollup(FacilityRecord.model_validate(doc))


# ---------------------------------------------------------------------------- the importer


class EpochImporter:
    name = "epoch"
    match_key = "epoch_name"
    owned_external_keys = ("epoch_name",)
    review_sources = ("epoch",)
    version = "1"
    help = "Epoch AI Frontier Data Centers (CC BY 4.0): US sites with dated timelines"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--overrides",
            type=Path,
            default=OVERRIDES_PATH,
            help="cited location overrides (default: config/overrides/epoch.json)",
        )
        parser.add_argument(
            "--no-geocode",
            action="store_true",
            help="skip the Census Geocoder; use only overrides, Gazetteer places and county names",
        )

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        from atlas.geocode import CensusGeocoder, Gazetteer, GeocoderError

        overrides = load_overrides(getattr(args, "overrides", OVERRIDES_PATH))

        def fetch_zip() -> FetchResult:
            from atlas.net import fetch

            return fetch(
                ctx.http,
                ZIP_URL,
                allowed_types=("application/zip", "application/octet-stream"),
                max_bytes=20_000_000,
            )

        _, snapshot = load_input(
            ctx, name=self.name, url=ZIP_URL, license=LICENSE, ext="zip", fetch=fetch_zip
        )
        snapshot = snapshot.model_copy(update={"upstream_version": snapshot.etag})
        zip_path = ctx.input_path or ctx.raw_path(self.name, snapshot.sha256, "zip")
        _, site_rows, raw_timeline = read_zip(zip_path)
        timelines = timeline_rows(raw_timeline)

        census = (
            None if getattr(args, "no_geocode", False) else CensusGeocoder(ctx.http, ctx.cache_dir)
        )
        gazetteer = Gazetteer.load()
        counties = ctx.counties()
        retrieved_at = snapshot.retrieved_at.isoformat()

        candidates: list[Candidate] = []
        review: list[ReviewItem] = []
        methods: Counter[str] = Counter()
        us_sites = 0

        def flag(kind: str, site: str, reason: str, **data: Any) -> None:
            review.append(
                ReviewItem(
                    source=self.name,
                    kind=kind,
                    external_id=site,
                    record_id=None,
                    reason=reason,
                    data=data,
                )
            )

        for row in site_rows:
            if clean_text(row["Country"]) != COUNTRY:
                continue
            us_sites += 1
            site = Site(
                name=clean_text(row["Name"]),
                address=clean_text(row["Address"]),
                owner=row["Owner"],
                users=row["Users"],
                project=row["Project"],
                selected_sources=row["Selected Sources"],
                capital_cost_billions=parse_number(
                    row["Current total capital cost (2025 USD billions)"]
                ),
            )
            if not site.name:
                continue
            rows = timelines.get(site.name, [])
            if not any(r.day <= ctx.today for r in rows):
                flag(
                    "unknown_status",
                    site.name,
                    "no timeline row dated today or earlier, so there is no current status",
                    timeline_rows=len(rows),
                )
                continue
            try:
                placed = self._place(site, overrides, census, gazetteer, counties)
            except GeocoderError as e:
                raise epoch_error(f"Census Geocoder: {e}") from e
            if placed is None:
                if site.address:
                    flag(
                        "geocode_failed",
                        site.name,
                        "the address did not geocode (Census, Gazetteer, county name)",
                        address=site.address,
                    )
                else:
                    flag(
                        "missing_location",
                        site.name,
                        "Epoch gives no address; add a cited entry to "
                        "config/overrides/epoch.json to import it",
                    )
                continue
            methods[placed.method] += 1
            try:
                record = site_record(site, rows, placed, ctx=ctx, retrieved_at=retrieved_at)
            except (ValidationError, RollupError) as e:
                flag("invalid", site.name, "the row does not map to a valid record", error=str(e))
                continue
            candidates.append(Candidate((site.name,), record))

        planned = sum(1 for c in candidates for e in c.record.status_history if e.planned)
        metrics: dict[str, float | int] = {
            "sites": len(site_rows),
            "us_sites": us_sites,
            "timeline_rows": len(raw_timeline),
            "planned_events": planned,
            "candidates": len(candidates),
            "missing_location": sum(1 for i in review if i.kind == "missing_location"),
            "geocode_failed": sum(1 for i in review if i.kind == "geocode_failed"),
        }
        names = {clean_text(r["Name"]) for r in site_rows}
        metrics["overrides_used"] = sum(1 for n in overrides if n in names)
        metrics["overrides_unused"] = sum(1 for n in overrides if n not in names)
        for method, n in sorted(methods.items()):
            metrics[f"location_{method}"] = n
        if census is not None:
            metrics["census_requests"] = census.requests
            metrics["census_cache_hits"] = census.cache_hits
        return ImportResult(self.name, [snapshot], candidates, review, metrics)

    @staticmethod
    def _place(
        site: Site,
        overrides: Mapping[str, LocationOverride],
        census: CensusGeocoder | None,
        gazetteer: Gazetteer,
        counties: CountyIndex,
    ) -> Placed | None:
        from atlas.geocode import GeocodeRequest, geocode, parse_address

        override = overrides.get(site.name)
        if override is not None:
            return place_override(site.name, override, counties=counties, gazetteer=gazetteer)
        if not site.address:
            return None
        parsed = parse_address(site.address)
        result = geocode(
            GeocodeRequest(
                state_abbr=parsed.state_abbr,
                oneline=parsed.text,
                street=parsed.street,
                city=parsed.city,
                postcode=parsed.postcode,
                locality=parsed.city,
                county_name=parsed.county_name,
            ),
            census=census,
            gazetteer=gazetteer,
            counties=counties,
        )
        if result is None:
            return None
        return Placed(
            _location_from_result(result),
            result.city
            or (gazetteer.county_full_name(result.county_fips) if result.county_fips else None),
            result.confidence,
            result.precision,
            None,
        )


IMPORTER = EpochImporter()
