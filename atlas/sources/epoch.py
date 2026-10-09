"""Epoch AI Frontier Data Centers: a pipeline seed (07 §4.1, §4.3).

Reads https://epoch.ai/data/data_centers/data_centers.zip (CC BY 4.0): README.md (the license is
checked: it must link the CC BY 4.0 deed and name no NonCommercial, NoDerivatives or ShareAlike
term), data_centers.csv (one row per site) and data_center_timelines.csv (dated rows per site).
Only US sites are imported. Each becomes a `project` (a `campus` once a building is operational)
whose status_history comes from the timeline through the status crosswalk (07 §4.7):

- rows are mapped with atlas.crosswalk.from_epoch_row, on the status text with its markdown links
  reduced to their text, and an event is kept only where the status changes;
- a row dates the start of construction only when its note says construction starts; the first
  row is otherwise a `first_reported` observation, and a later one an `other` event;
- rows dated after today are Epoch's projections and become planned events, which never set the
  status (07 §2.3);
- capacity and water use come from the latest row dated today or earlier.

Epoch's Owner column is the owner of the AI hardware, "not necessarily the owner or operator of
the facility" (Epoch's field definitions). It is published with Users as `parties.tenant`, the
companies whose computing the facility houses; Epoch names no facility owner or operator.

Epoch tracks the buildings it counts as AI compute, and a site can be buildings added to an older
campus (07 §2.1: an expansion is a phase). When a note says so (an expansion, a conversion of
existing buildings, a building Epoch leaves out of its count), or the first row already counts
operational buildings, the timeline does not date the facility: its rows become `other` events on
a phase for the tracked buildings, which carries the capacity, and the record gets no
construction or operating date, capacity, water use or cost of its own. A site whose cited sources
only mention an expansion is treated the same way and held for review (`conflict`), since Epoch's
fields do not say which campus the expansion belongs to. Sites with the same street address are
one campus, with a phase per site.

The CSV has an address but no coordinates. A cited entry in config/overrides/epoch.json wins;
otherwise the address goes through atlas.geocode (Census Geocoder, then the county by name). The
address's city is its postal city, so it is sent only as the city a Census match must agree with
(and the point of the postal city inside a county the address states), never as the place the site
is in: the record's city is the Census place that contains a Census match's point (07 §6.5). Sites
with no address and no override become `missing_location` review items, and sites the chain cannot
place become `geocode_failed` items; neither becomes a record, and a cited override places them.

Importing this module does no I/O.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, HttpUrl, TypeAdapter, ValidationError

from atlas.crosswalk import Crosswalked, from_epoch_row
from atlas.schema.record import (
    PLACEHOLDER_ID,
    AtlasModel,
    EventType,
    FacilityRecord,
    Precision,
    SourceType,
)
from atlas.schema.rollup import RollupError, apply_rollup
from atlas.sources.base import Candidate, ImportResult, ReviewItem, classify_source, load_input
from atlas.text import clean_text as _clean_text
from atlas.text import find_personal_data, strip_invisible

if TYPE_CHECKING:
    from atlas.geo.counties import CountyIndex
    from atlas.geo.places import PlaceIndex
    from atlas.geocode import CensusGeocoder, Gazetteer, GeocodeResult
    from atlas.net import FetchError, FetchResult
    from atlas.sources.base import ImportContext

ZIP_URL = "https://epoch.ai/data/data_centers/data_centers.zip"
DATASET_URL = "https://epoch.ai/data/ai-data-centers"
LICENSE = "CC-BY-4.0"
# README.md must link this deed, and must not name a more restrictive Creative Commons license
# (all of whose names also start "Creative Commons Attribution").
LICENSE_URI = "creativecommons.org/licenses/by/4.0"
_RESTRICTED_LICENSE_RE = re.compile(
    r"\bby-(?:nc|nd|sa)\b|non-?commercial|no-?deriv|share-?alike", re.I
)
OVERRIDES_PATH = Path("config/overrides/epoch.json")
COUNTRY = "United States"
MAX_LINK_SOURCES = 5
NOTE_MAX = 200
CONFIDENCE = 0.70
# A location an override cites with a verbatim quote: 07 §3.4 "stated, exact quote" (0.85) plus the
# source adjustment for the cited source's type.
OVERRIDE_CONFIDENCE = 0.85
SOURCE_ADJUSTMENT: dict[str, float] = {
    "government_record": 0.10,
    "court_or_regulator": 0.10,
    "utility_or_iso_filing": 0.07,
    "sec_filing": 0.07,
    "company_release": 0.05,
}

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
# A note that says construction starts: "Land clearing begins", "Construction start",
# "groundbreaking", "Building 1 foundation started", "First signs of construction". Other notes
# observe work under way ("Land is cleared", "Cooling install continues on the roof").
_START_RE = re.compile(
    r"\b(?:begins?|began|begun|beginning|starts?|started|starting|commenc\w*|first signs)\b"
    r"|\bground ?break\w*|\bbr(?:oke|eaks?) ground\b|\bground (?:is |was |has been )?broken\b",
    re.I,
)
# Notes, on any row dated today or earlier, that say the buildings Epoch tracks were converted
# from or added to a facility that was already there, or that the site has a building Epoch
# leaves out of its count (seed input 2026-10-08): "converting existing Bitcoin mining buildings",
# "rebuilding their existing Dalton 1 (and we're assuming 2) datacenter", "The former crypto
# mining Helios site", "The data center was formerly owned by Capital One", "Building 1, which was
# first operational in 2021", "this multi-tenant building", "245 MW of Bitcoin-mining capacity",
# "the non AI building (PX1)", "we suspect this building is not for AI compute". Between
# "existing" and the noun only names, numbers and a parenthesis may stand, so "an existing
# substation to the northeast of the data center" does not count.
_PARTIAL_RE = re.compile(
    r"(?i:\bexisting)\s+(?:(?:[A-Z0-9][\w'-]*|\([^)]*\))\s+){0,4}"
    r"(?i:data ?cent(?:er|re)s?\b|datacenters?\b|bitcoin\b|crypto)"
    r"|(?i:\bformer (?:crypto|bitcoin)\b|\b(?:data ?cent(?:er|re)|datacenter) was formerly\b"
    r"|\bfirst operational in (?:19|20)[0-9]{2}\b|\bmulti-tenant building\b"
    r"|\b(?:bitcoin|crypto)[- ]mining\b|\bnon[- ]AI building\b|\bnot for AI\b)"
)
# The same, on the first row only, where it describes the start of Epoch's tracking: "Land cleared
# for site expansion", "Building 1 of expansion", "the ... site begins expanding", "Land clearing
# starts for new building". On a later row these words describe the tracked site's own growth.
_PARTIAL_FIRST_RE = re.compile(
    r"\b(?:site|campus) expansion\b|\bof (?:the )?expansion\b|\bbegins expanding\b"
    r"|\bnew building\b",
    re.I,
)
# A cited source, or the site's own name, that mentions an expansion. It may be this site's future
# growth ("Plan for 9-building and 6-building expansions") or the reason the site exists ("Campus
# extension announcement"): Epoch's fields do not say which, so the site is held for review.
_EXPANSION_RE = re.compile(r"\bexpansions?\b|\bextension\b", re.I)
PHASE_NAME = "{name} (buildings tracked by Epoch AI)"


def epoch_error(message: str) -> FetchError:
    """The Epoch download or the overrides file cannot be used (license, layout or content).

    A FetchError, so `atlas import` reports it and exits 1; atlas.net (httpx) loads only here.
    """
    from atlas.net import FetchError

    return FetchError(message)


class LocationOverride(AtlasModel):
    """One entry of config/overrides/epoch.json: a location taken from a cited source.

    quote is the sentence (or the shortest span of it, at most 300 characters) that states the
    location, copied verbatim from source_url when it was read at retrieved_at; archive_url is the
    snapshot that was read when the live page refuses automated clients.
    """

    state_abbr: str = Field(pattern=r"^[A-Z]{2}$")
    county_fips: str | None = Field(None, pattern=r"^\d{5}$")
    city: str | None = None
    municipality: str | None = None
    lat: float | None = Field(None, ge=18, le=72)
    lon: float | None = Field(None, ge=-180, le=-64)
    precision: Precision
    source_url: HttpUrl
    archive_url: HttpUrl | None = None
    quote: str = Field(min_length=1, max_length=300)
    retrieved_at: AwareDatetime
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
    return _clean_text(s)


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
    check_license(readme)
    return (
        readme,
        _rows(sites, SITE_COLUMNS, "data_centers.csv"),
        _rows(timelines, TIMELINE_COLUMNS, "data_center_timelines.csv"),
    )


def check_license(readme: str) -> None:
    """Raise unless README.md grants CC BY 4.0: the deed is linked, and no NonCommercial,
    NoDerivatives or ShareAlike term appears (a switch to one of those would bar republishing the
    data in an ODbL database)."""
    restricted = _RESTRICTED_LICENSE_RE.search(readme)
    if LICENSE_URI not in readme.casefold() or restricted:
        why = f"names {restricted.group(0)!r}" if restricted else f"does not link {LICENSE_URI}"
        raise epoch_error(
            f"Epoch README.md {why}, so the data may no longer be CC BY 4.0; check the license "
            "before importing"
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


def row_text(row: TimelineRow) -> str:
    """The construction-status text as the crosswalk reads it: links reduced to their text, so a
    URL ("...Galaxy-Announces-Commitment...") never decides the status."""
    return clean_text(strip_links(row.status_text))


def row_operational(row: TimelineRow) -> bool:
    """The crosswalk's reading: a blank building count falls back to the row's IT power."""
    count = row.buildings_operational if row.buildings_operational is not None else row.it_mw
    return (count or 0) > 0


def _event_type(
    cw: Crosswalked, text: str, *, planned: bool, first: bool, observed: bool
) -> EventType:
    """The event a row adds. A projection keeps the crosswalk's event, which never dates anything.
    An observed timeline (one that does not date the facility) adds `other` events. Otherwise a
    first row that already counts operational buildings is an observation (`other`: Epoch began
    tracking a site that was running, so it dates neither the first report nor the start of
    operation), and an under-construction row dates the start only when its note says
    construction starts: else the first row is `first_reported` and a later one `other`."""
    if planned:
        return cw.event
    if observed or (first and cw.status == "operating"):
        return "other"
    if cw.event == "construction_start" and not _START_RE.search(text):
        return "first_reported" if first else "other"
    return cw.event


def status_events(
    rows: Sequence[TimelineRow],
    today: date,
    *,
    observed: bool = False,
    phase_id: str | None = None,
) -> list[dict[str, Any]]:
    """One event per status change; rows after today are planned (07 §2.3). With observed=True
    every row dated today or earlier is an `other` event (see _event_type)."""
    events: list[dict[str, Any]] = []
    previous: str | None = None
    for row in rows:
        text = row_text(row)
        cw = from_epoch_row(text, row.buildings_operational, it_mw=row.it_mw)
        if cw.status == previous:
            continue
        previous = cw.status
        planned = row.day > today
        event: dict[str, Any] = {
            "seq": len(events) + 1,
            "status": cw.status,
            "event": _event_type(cw, text, planned=planned, first=not events, observed=observed),
            "as_of": {"value": row.day.isoformat(), "precision": "day"},
            "phase_id": phase_id,
            "planned": planned,
            "source_ids": ["s1"],
            "note": event_note(text),
        }
        events.append(event)
    return events


@dataclass(frozen=True)
class Coverage:
    """Whether a site's timeline dates the whole facility (07 §2.1).

    partial: Epoch's notes say the tracked buildings were converted from or added to an older
    facility, or leave a building out of the count, or the first row already counts operational
    buildings. held: a cited source or the name mentions an expansion, which Epoch's fields do not
    place; the site is treated as partial and held for review. evidence: what decided it.
    """

    partial: bool = False
    held: bool = False
    evidence: str | None = None

    @property
    def observed(self) -> bool:
        return self.partial or self.held


def timeline_coverage(
    members: Sequence[tuple[Site, Sequence[TimelineRow]]], today: date
) -> Coverage:
    """How far the timelines of one campus's sites date the campus (see Coverage). Every site's
    notes count; the cited sources and the name only of the first site, since the others are its
    phases and their sources describe them as such."""

    def evidence(row: TimelineRow) -> str:
        return f"{row.day.isoformat()}: {event_note(row_text(row)) or ''}".rstrip()

    for _site, rows in members:
        actual = [r for r in rows if r.day <= today]
        if actual and row_operational(actual[0]):
            return Coverage(partial=True, evidence=evidence(actual[0]))
        for i, r in enumerate(actual):
            text = row_text(r)
            if _PARTIAL_RE.search(text) or (i == 0 and _PARTIAL_FIRST_RE.search(text)):
                return Coverage(partial=True, evidence=evidence(r))
    primary = members[0][0]
    for title in (primary.name, *(t for t, _ in selected_links(primary.selected_sources))):
        if _EXPANSION_RE.search(title):
            return Coverage(held=True, evidence=title)
    return Coverage()


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
        "municipality": r.municipality,
        "geocode_method": r.method,
    }


def place_override(
    name: str, ov: LocationOverride, *, counties: CountyIndex, gazetteer: Gazetteer
) -> Placed:
    """The location an override describes, checked like `atlas validate` rule 5 would; a point
    never lies outside the county the entry names, at any precision (as in atlas.geocode)."""
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
        elif county is not None and not counties.contains(county.fips, lat, lon):
            # A record never names a county its point lies outside of (the geocode chain's rule).
            point = (
                f"the Gazetteer place {ov.city!r}" if method == "gazetteer" else f"({lat}, {lon})"
            )
            raise bad(f"{point} is not inside county {county.fips}")
    location = {
        "lat": lat,
        "lon": lon,
        "precision": ov.precision,
        "city": ov.city,
        "municipality": ov.municipality,
        "county_name": county.name if county else None,
        "county_fips": county.fips if county else None,
        "state_abbr": ov.state_abbr,
        "geocode_method": method,
    }
    where = (
        ov.city or ov.municipality or (gazetteer.county_full_name(county.fips) if county else None)
    )
    return Placed(location, where, override_confidence(ov), "override", ov)


def override_source_type(ov: LocationOverride) -> SourceType:
    return ov.source_type or classify_source(str(ov.source_url))


def override_confidence(ov: LocationOverride) -> float:
    """07 §3.4: stated with a verbatim quote (0.85) plus the source adjustment, at most 0.99."""
    adjustment = SOURCE_ADJUSTMENT.get(override_source_type(ov), 0.0)
    return round(min(0.99, OVERRIDE_CONFIDENCE + adjustment), 2)


def phase_slug(name: str) -> str:
    """A phase id from a site name: "OpenAI Stargate Abilene" -> "openai-stargate-abilene"."""
    return re.sub(r"[^a-z0-9]+", "-", clean_text(name).casefold()).strip("-") or "epoch"


def _latest_actual(rows: Sequence[TimelineRow], today: date) -> TimelineRow | None:
    actual = [r for r in rows if r.day <= today]
    return actual[-1] if actual else None


def _capacity(row: TimelineRow | None) -> dict[str, float]:
    """IT power and power of a row, each when > 0."""
    if row is None:
        return {}
    pairs = (("it_mw", positive(row.it_mw)), ("facility_mw", positive(row.power_mw)))
    return {k: v for k, v in pairs if v is not None}


def _unique(names: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        if n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


def site_record(
    site: Site,
    rows: Sequence[TimelineRow],
    placed: Placed,
    *,
    ctx: ImportContext,
    retrieved_at: str,
) -> FacilityRecord:
    """The candidate record for one site (id PLACEHOLDER_ID, rollup applied)."""
    return campus_record([(site, rows)], placed, ctx=ctx, retrieved_at=retrieved_at)


def campus_record(
    members: Sequence[tuple[Site, Sequence[TimelineRow]]],
    placed: Placed,
    *,
    ctx: ImportContext,
    retrieved_at: str,
    coverage: Coverage | None = None,
) -> FacilityRecord:
    """The candidate record for one campus: one Epoch site, or several at one street address
    (the first names the record; each is a phase). id PLACEHOLDER_ID, rollup applied.

    A timeline that does not date the facility (coverage.observed) becomes `other` events on a
    phase for the tracked buildings, which carries the capacity; the record then has no capacity,
    water use or cost of its own, and no construction or operating date.
    """
    coverage = coverage if coverage is not None else timeline_coverage(members, ctx.today)
    observed = coverage.observed
    phased = observed or len(members) > 1
    primary = members[0][0]

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
                "archive_url": str(ov.archive_url) if ov.archive_url else None,
                "publisher": ov.publisher or host_of(str(ov.source_url)),
                "title": None,
                "source_type": override_source_type(ov),
                "retrieved_at": ov.retrieved_at.isoformat(),
                "quote": ov.quote,
                "quote_match": "human",
                "supports": ["/location"],
            }
        )
    cited = {str(HttpUrl(str(s["url"]))) for s in sources}
    for site, _rows in members:
        links = [
            (t, u) for t, u in selected_links(site.selected_sources) if str(HttpUrl(u)) not in cited
        ]
        for title, url in links[:MAX_LINK_SOURCES]:
            cited.add(str(HttpUrl(url)))
            sources.append(
                {
                    "id": f"s{len(sources) + 1}",
                    "url": url,
                    "publisher": host_of(url),
                    "title": title or None,
                    "source_type": classify_source(url, title),
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
    events: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    water = 0.0
    cost = 0.0
    operational = False
    phase_ids: set[str] = set()
    for order, (site, rows) in enumerate(members):
        pid: str | None = None
        if phased:
            pid = phase_slug(site.name)
            if pid in phase_ids:  # two names that differ only in punctuation
                pid = f"{pid}-{order + 1}"
            phase_ids.add(pid)
        for e in status_events(rows, ctx.today, observed=observed, phase_id=pid):
            events.append({**e, "seq": (e["as_of"]["value"], order, e["seq"])})
        latest = _latest_actual(rows, ctx.today)
        site_capacity = _capacity(latest)
        operational = operational or any(row_operational(r) for r in rows if r.day <= ctx.today)
        if pid is not None:
            phases.append(
                {
                    "phase_id": pid,
                    "name": PHASE_NAME.format(name=site.name),
                    "capacity": site_capacity,
                    "source_ids": ["s1"],
                }
            )
        for key, mw in site_capacity.items():
            totals[key] = totals.get(key, 0.0) + mw
        water += (positive(latest.water_mgd) if latest is not None else None) or 0.0
        cost += positive(site.capital_cost_billions) or 0.0
    # seq follows the date, then the order of the sites, then the order within a site.
    events.sort(key=lambda e: e["seq"])
    for i, e in enumerate(events, start=1):
        e["seq"] = i

    capacity: dict[str, Any] = {}
    cooling: dict[str, Any] = {}
    money: dict[str, Any] = {}
    if not observed:
        for key in ("it_mw", "facility_mw"):
            if key in totals:
                capacity[key] = totals[key]
                field_meta[f"/capacity/{key}"] = imported
        if water > 0:
            cooling["water_use_mgd"] = water
            field_meta["/cooling/water_use_mgd"] = imported
        if cost > 0:
            money = {
                "investment_usd": float(round(cost * 1e9)),
                "investment_basis": "estimate",
                "currency_year": 2025,
            }
            field_meta["/money/investment_usd"] = imported

    ref = {"source_ids": ["s1"]}
    # Epoch's Owner owns the AI hardware, "not necessarily the owner or operator of the facility":
    # with Users (the AI labs using the compute) it is a company the facility houses, a tenant.
    tenants = _unique(
        n for s, _ in members for n in (*tagged_names(s.owner), *tagged_names(s.users))
    )
    parties = {"tenant": [{"name": n, **ref} for n in tenants]}
    codenames = _unique(n for s, _ in members for n in tagged_names(s.project))
    aliases = [{"name": n, "kind": "codename", **ref} for n in codenames]
    aliases += [{"name": s.name, "kind": "phase_name", **ref} for s, _ in members[1:]]
    where = placed.city_or_county
    st = placed.location["state_abbr"]
    canonical = f"{primary.name} ({where}, {st})" if where else f"{primary.name} ({st})"
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
        "phases": phases,
        "cooling": cooling,
        "money": money,
        "external_ids": {"epoch_name": [s.name for s, _ in members]},
        "sources": sources,
        "field_meta": field_meta,
        "review": {"state": "machine"},
        "created_at": ts,
        "updated_at": ts,
        "last_verified_at": ts,
    }
    return apply_rollup(FacilityRecord.model_validate(doc))


def coverage_review(
    record: FacilityRecord, coverage: Coverage
) -> tuple[str, dict[str, Any]] | None:
    """The review item (reason, data) a campus's coverage calls for, if any.

    A held site: Epoch's fields do not say whether its timeline is the facility's, so the dates
    and capacity it would give the facility are left out. A partial timeline whose tracked
    buildings are not operating: the facility (older, or with a building Epoch does not count) may
    be further along than the status Epoch's buildings give it.
    """
    data: dict[str, Any] = {"evidence": coverage.evidence, "status": record.status}
    if coverage.held:
        return (
            "a cited source or the name mentions an expansion, so Epoch's timeline may cover only "
            "buildings added to an older campus: the record gives no construction or operating "
            "date and puts the capacity on the phase; restore them if the timeline covers the "
            "whole facility",
            data,
        )
    if coverage.partial and record.status != "operating":
        return (
            "Epoch's timeline covers only part of the facility, so the facility may be further "
            f"along than the {record.status} of the buildings Epoch tracks",
            data,
        )
    return None


def address_key(address: str) -> str | None:
    """A street address as a campus key: case, punctuation and spacing folded. None unless it
    starts with a house number: a town or a road alone is no address two sites can share."""
    text = re.sub(r"[^\w]+", " ", clean_text(address).casefold()).strip()
    text = re.sub(r" (?:usa|united states)$", "", text)
    return text if re.match(r"[0-9]+[a-z]?\b", text) else None


# ---------------------------------------------------------------------------- the importer


class EpochImporter:
    name = "epoch"
    match_key = "epoch_name"
    owned_external_keys = ("epoch_name",)
    review_sources = ("epoch",)
    version = "3"
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
        from atlas.geocode import CensusGeocoder, Gazetteer, GeocoderError, parse_address

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
        # The Census place polygons name the city of a Census match (downloaded on first use);
        # without the Census step nothing needs them. The Gazetteer gives county names, override
        # places and a postal city's point: Epoch states no town or township, so it needs no
        # county subdivisions.
        places = ctx.places() if census is not None else None
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

        def unplaced(site: Site) -> None:
            parsed = parse_address(site.address) if site.address else None
            if parsed is not None and parsed.state_abbr is None:
                flag(
                    "geocode_failed",
                    site.name,
                    "the address names no state, so it is not geocoded (a Census match "
                    "could come from any state); add a cited entry to "
                    "config/overrides/epoch.json to import it",
                    address=site.address,
                )
            elif parsed is not None:
                data: dict[str, Any] = {"address": site.address}
                refused = census.seen(parsed.text) if census is not None else []
                if refused:
                    data["census_matches"] = [m.matched_address for m in refused]
                matched = "no agreeing Census match" if census is not None else "no Census step"
                if parsed.city and not parsed.county_name:
                    reason = (
                        f"{matched}, and the address's city ({parsed.city}) is only its postal "
                        "city, which gives no point without a county a source states (07 §6.5); "
                        "a cited entry in config/overrides/epoch.json places it"
                    )
                else:
                    reason = (
                        f"the address did not geocode: {matched}, and neither a county it names "
                        "nor its postal city gives a point; a cited entry in "
                        "config/overrides/epoch.json places it"
                    )
                flag("geocode_failed", site.name, reason, **data)
            else:
                flag(
                    "missing_location",
                    site.name,
                    "Epoch gives no address; add a cited entry to "
                    "config/overrides/epoch.json to import it",
                )

        # Sites with the same street address are one campus (07 §2.1), in the CSV's order.
        campuses: dict[str, list[tuple[Site, list[TimelineRow]]]] = {}
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
            key = address_key(site.address) or f"site:{site.name}"
            campuses.setdefault(key, []).append((site, rows))

        coverages: Counter[str] = Counter()
        for members in campuses.values():
            site = next((s for s, _ in members if s.name in overrides), members[0][0])
            try:
                placed = self._place(site, overrides, census, gazetteer, counties, places)
            except GeocoderError as e:
                raise epoch_error(f"Census Geocoder: {e}") from e
            if placed is None:
                for s, _ in members:
                    unplaced(s)
                continue
            methods[placed.method] += 1
            coverage = timeline_coverage(members, ctx.today)
            primary = members[0][0].name
            try:
                record = campus_record(
                    members, placed, ctx=ctx, retrieved_at=retrieved_at, coverage=coverage
                )
            except (ValidationError, RollupError) as e:
                flag("invalid", primary, "the row does not map to a valid record", error=str(e))
                continue
            candidates.append(Candidate(tuple(s.name for s, _ in members), record))
            coverages["held" if coverage.held else "partial" if coverage.partial else "whole"] += 1
            item = coverage_review(record, coverage)
            if item is not None:
                flag("conflict", primary, item[0], **item[1])

        planned = sum(1 for c in candidates for e in c.record.status_history if e.planned)
        metrics: dict[str, float | int] = {
            "sites": len(site_rows),
            "us_sites": us_sites,
            "timeline_rows": len(raw_timeline),
            "planned_events": planned,
            "candidates": len(candidates),
            "sites_in_shared_campuses": sum(
                len(c.match_values) for c in candidates if len(c.match_values) > 1
            ),
            "timelines_partial": coverages["partial"],
            "timelines_held": coverages["held"],
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
        places: PlaceIndex | None = None,
    ) -> Placed | None:
        """An override, else the address through geocode(). The address's city is a postal
        city (Berwick, PA for a campus in Salem Township, Luzerne County; Fairfax, IA for one in
        Cedar Rapids), so it is the city a Census match must agree with, and it gives its point
        only inside a county the address states; it is never sent as the locality, which would
        name it as the place the site is in (07 §6.5)."""
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
                county_name=parsed.county_name,
            ),
            census=census,
            gazetteer=gazetteer,
            counties=counties,
            places=places,
        )
        if result is None:
            return None
        return Placed(
            _location_from_result(result),
            result.city
            or result.municipality
            or (gazetteer.county_full_name(result.county_fips) if result.county_fips else None),
            result.confidence,
            result.precision,
            None,
        )


IMPORTER = EpochImporter()
