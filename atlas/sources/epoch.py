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
- a date on the 1st of a month, or on the 15th or the last day with a note that says it is
  estimated, is read as the month (Epoch writes its estimates there);
- a cited `first_report` (an override entry) earlier than every row dates first_reported instead
  of Epoch's first row, which observes what imagery shows rather than the first public report;
- capacity and water use come from the latest row dated today or earlier.

Epoch's Owner column is the owner of the AI hardware, "not necessarily the owner or operator of
the facility" (Epoch's field definitions). It is published with Users as `parties.tenant`, the
companies whose computing the facility houses; Epoch names no facility owner or operator.

Epoch tracks the buildings it counts as AI compute, and a site can be buildings added to an older
campus or converted from a bitcoin site (07 §2.1: an expansion is a phase). When a note or link
title says so (an expansion, a conversion of existing or owned infrastructure, a building Epoch
leaves out of its count, only part of a campus counted), the first row already counts operational
buildings, or a cited `timeline` entry says so, the timeline does not date the facility: its rows
become `other` events on a phase for the tracked buildings, which carries the capacity, and the
record gets no construction or operating date, capacity, water use or cost of its own. A site that
Epoch's fields only suggest is one (a cited source or the name mentions an expansion, the name or
the first row names part of a site, a bitcoin miner owns or hosts it) is treated the same way and
held for review (`conflict`). When such a timeline's tracked buildings are not operating, their
status is not the facility's: there is no record (a `conflict` item says why) unless a cited
`facility_status` entry gives the facility's status, on a phase of the buildings Epoch does not
track. Sites with the same street address are one campus, with a phase per site.

The CSV has an address but no coordinates. A cited entry in config/overrides/epoch.json wins (a
municipality at its county subdivision's Gazetteer point; a city that is also the address's postal
city only with a place_check, how the site was found inside the city's place polygon);
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
import calendar
import csv
import io
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from atlas.crosswalk import Crosswalked, from_epoch_row
from atlas.schema.record import (
    PLACEHOLDER_ID,
    AtlasModel,
    EventType,
    FacilityRecord,
    FuzzyDate,
    Precision,
    SourceType,
    Status,
)
from atlas.schema.rollup import ACTIVE_ORDER, RollupError, apply_rollup, period_start
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
# substation to the northeast of the data center" does not count. Also (seed input 2026-10-09):
# "about 70 MW of HPC infrastructure from 100 MW of owned infrastructure. Site modifications were
# expected" (a bitcoin site's conversion), "we're only including roughly 44% of all the New Albany
# campuses IT Power", and a Selected Sources title, "a set of three long buildings, which includes
# the first building we believe is for AI". A retrofit or conversion alone ("retrofitting a former
# Electrolux building") says nothing of an older data center.
_PARTIAL_RE = re.compile(
    r"(?i:\bexisting)\s+(?:(?:[A-Z0-9][\w'-]*|\([^)]*\))\s+){0,4}"
    r"(?i:data ?cent(?:er|re)s?\b|datacenters?\b|bitcoin\b|crypto)"
    r"|(?i:\bformer (?:crypto|bitcoin)\b|\b(?:data ?cent(?:er|re)|datacenter) was formerly\b"
    r"|\bfirst operational in (?:19|20)[0-9]{2}\b|\bmulti-tenant building\b"
    r"|\b(?:bitcoin|crypto)[- ]mining\b|\bnon[- ]AI building\b|\bnot for AI\b"
    r"|\bsite modifications?\b|\bof owned infrastructure\b|\bonly (?:including|counting)\b"
    r"|\bbelieve (?:is|are) for AI\b)"
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
# A site name that names part of a site: "Google Council Bluffs (East)", "Google Pryor (North)".
_PART_NAME_RE = re.compile(r"\((?:north|south|east|west)(?:[- ]?(?:east|west))?\)\s*$", re.I)
# A first row that starts with some of a site's buildings, not its first: "Land clearing begins for
# east buildings" (Council Bluffs East, beside Google's older campus), "for Buildings 5-9 (eastern
# campus)". "for northern two buildings (Building 1-2)" names the first ones, so it does not count.
_PART_BUILDINGS_RE = re.compile(r"\bfor (?:the )?(?:north|south|east|west)\w* buildings\b", re.I)
_BUILDING_NUMBER_RE = re.compile(r"\bbuildings? ([0-9]+)", re.I)
# A site that a bitcoin miner owns or hosts (named in the site's name, Owner, Users, Project,
# Selected Sources titles or notes): its AI buildings may be converted from, or added to, a mining
# facility, which Epoch's fields do not say (CoreWeave Marble: Core Scientific's site, mining since
# 2018). Held for review.
_MINER_RE = re.compile(
    r"\b(?:Core Scientific|TeraWulf|Galaxy Digital|Cipher Mining|Hut 8|Riot Platforms|"
    r"Bitfarms|CleanSpark|Marathon Digital|Applied Digital|Bit Digital|Bitdeer|Greenidge|"
    r"Soluna|Mawson|Stronghold Digital|Northern Data|HIVE Digital|Iris Energy)\b|\b(?:IREN|MARA)\b",
)
# Notes that give a date as an estimate: Epoch writes those on the 1st of a month (83 of the 452 US
# rows on 2026-10-09 fall on the 1st, about 12 on any other day), and often on the 15th or the last
# day ("Building 3 is operational (estimate)", 2026-06-30).
_ESTIMATE_RE = re.compile(r"\bestimat\w*|\bassum\w*|\bbest guess\b|\bwe (?:expect|think)\b", re.I)
PHASE_NAME = "{name} (buildings tracked by Epoch AI)"
UNTRACKED_PHASE_NAME = "{name} (buildings Epoch AI does not track)"


def epoch_error(message: str) -> FetchError:
    """The Epoch download or the overrides file cannot be used (license, layout or content).

    A FetchError, so `atlas import` reports it and exits 1; atlas.net (httpx) loads only here.
    """
    from atlas.net import FetchError

    return FetchError(message)


class Citation(AtlasModel):
    """A page an override entry cites: quote is the sentence (or the shortest span of it, at most
    300 characters) that states what the entry takes from it, copied verbatim from source_url when
    it was read at retrieved_at; archive_url is the snapshot that was read when the live page
    refuses automated clients; note says why this source."""

    source_url: HttpUrl
    archive_url: HttpUrl | None = None
    quote: str = Field(min_length=1, max_length=300)
    retrieved_at: AwareDatetime
    note: str = Field(min_length=1, max_length=300)
    publisher: str | None = None
    source_type: SourceType | None = None


class LocationOverride(Citation):
    """The location part of an entry of config/overrides/epoch.json, taken from a cited source.

    place_check: when the entry's city is also the postal city of Epoch's address, how the site
    was found inside that city's Census place polygon (a postal city is only near the site, 07
    §6.5: Google Pryor (North) is mailed to Pryor but lies in Mayes County); without it such an
    entry fails the run.
    """

    state_abbr: str = Field(pattern=r"^[A-Z]{2}$")
    county_fips: str | None = Field(None, pattern=r"^\d{5}$")
    city: str | None = None
    municipality: str | None = None
    lat: float | None = Field(None, ge=18, le=72)
    lon: float | None = Field(None, ge=-180, le=-64)
    precision: Precision
    place_check: str | None = Field(None, min_length=1, max_length=300)


class TimelineOverride(Citation):
    """Whether Epoch's timeline dates the facility, as a reviewer read it in a cited source:
    "partial" when the facility is older or larger than the buildings Epoch tracks (Meta Sarpy:
    "2017 Broke ground on the Sarpy Data Center", Epoch's Building 1 from 2024), "whole" when the
    timeline is the whole facility although a rule would hold it (07 §2.1)."""

    coverage: Literal["partial", "whole"]


class FirstReport(Citation):
    """A dated report of the facility earlier than Epoch's first row (Epoch's timeline starts
    with what its imagery shows, not with the first public report): published_at as the page
    states it (a date, or the time it gives), status what it reports (announced, proposed or
    permitted). It dates first_reported when it is earlier than every event of a timeline that
    dates the facility. source_url may be the entry's location source or a Selected Source."""

    published_at: AwareDatetime | date
    status: Literal["announced", "proposed", "permitted"]

    @property
    def day(self) -> date:
        p = self.published_at
        return p.date() if isinstance(p, datetime) else p


class FacilityStatus(Citation):
    """The facility's status on as_of, from a cited source, for a site whose Epoch timeline does
    not date the facility: it goes on a phase of the buildings Epoch does not track, as an `other`
    event that dates nothing (Google Fort Wayne: operating on 2025-12-11, while Epoch's buildings
    are under construction)."""

    status: Status
    as_of: date


_LOCATION_KEYS = frozenset(LocationOverride.model_fields)


class SiteOverride(AtlasModel):
    """One entry of config/overrides/epoch.json, keyed by Epoch's site name: a location (its
    fields at the top level of the entry), and any of timeline, first_report and
    facility_status (each with its own citation)."""

    location: LocationOverride | None = None
    timeline: TimelineOverride | None = None
    first_report: FirstReport | None = None
    facility_status: FacilityStatus | None = None

    @model_validator(mode="before")
    @classmethod
    def _flat_location(cls, data: Any) -> Any:
        if isinstance(data, dict) and "location" not in data:
            location = {k: v for k, v in data.items() if k in _LOCATION_KEYS}
            data = {k: v for k, v in data.items() if k not in _LOCATION_KEYS}
            if location:
                data["location"] = location
        return data

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not (self.location or self.timeline or self.first_report or self.facility_status):
            raise ValueError("an entry needs a location, timeline, first_report or facility_status")
        return self


_OVERRIDES = TypeAdapter(dict[str, SiteOverride])


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


def load_overrides(path: Path) -> dict[str, SiteOverride]:
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
    cw: Crosswalked,
    text: str,
    *,
    planned: bool,
    first: bool,
    observed: bool,
    reported: bool = False,
) -> EventType:
    """The event a row adds. A projection keeps the crosswalk's event, which never dates anything.
    An observed timeline (one that does not date the facility) adds `other` events. Otherwise a
    first row that already counts operational buildings is an observation (`other`: Epoch began
    tracking a site that was running, so it dates neither the first report nor the start of
    operation), and an under-construction row dates the start only when its note says
    construction starts: else the first row is `first_reported` and a later one `other`. With
    reported=True (an earlier dated report gives first_reported) the first row is `other` too."""
    if planned:
        return cw.event
    if observed or (first and cw.status == "operating"):
        return "other"
    if cw.event == "construction_start" and not _START_RE.search(text):
        return "first_reported" if first and not reported else "other"
    return cw.event


def row_date(row: TimelineRow, text: str | None = None) -> dict[str, str]:
    """A row's date as an event's as_of. Epoch writes a date it estimates on the 1st of a month
    (Google Kansas City East, 2026-08-01: "Building 1 operational. Estimated based on present
    construction progress and typical timelines"; DCD dates Google Mesa's groundbreaking 2023-07-12,
    Epoch 2023-07-01), so a date on the 1st is read as the month, as for AI GridWatch. So is one on
    the 15th or the last day of a month whose note says it is estimated or assumed ("Building 3 is
    operational (estimate)", 2026-06-30). Any other date is the day."""
    day = row.day
    note = row_text(row) if text is None else text
    month_end = day.day in (15, calendar.monthrange(day.year, day.month)[1])
    if day.day == 1 or (month_end and _ESTIMATE_RE.search(note)):
        return {"value": f"{day.year:04d}-{day.month:02d}", "precision": "month"}
    return {"value": day.isoformat(), "precision": "day"}


def status_events(
    rows: Sequence[TimelineRow],
    today: date,
    *,
    observed: bool = False,
    phase_id: str | None = None,
    reported: bool = False,
) -> list[dict[str, Any]]:
    """One event per status change; rows after today are planned (07 §2.3). With observed=True
    every row dated today or earlier is an `other` event (see _event_type); with reported=True
    (an earlier dated report gives first_reported) no row is a `first_reported` event."""
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
            "event": _event_type(
                cw, text, planned=planned, first=not events, observed=observed, reported=reported
            ),
            "as_of": row_date(row, text),
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

    partial: Epoch's notes or link titles say the tracked buildings were converted from or added
    to an older facility, or leave a building out of the count, or the first row already counts
    operational buildings, or a reviewer's cited `timeline` entry says so. held: Epoch's fields
    suggest it and do not decide it (a cited source or the name mentions an expansion, the name or
    the first row names part of a site, a bitcoin miner owns or hosts the site); the site is
    treated as partial and held for review. why: what kind of signal held it, for the review item.
    evidence: what decided it. cited: a `timeline` entry decided it.
    """

    partial: bool = False
    held: bool = False
    evidence: str | None = None
    why: str | None = None
    cited: bool = False

    @property
    def observed(self) -> bool:
        return self.partial or self.held


def _first_building_number(text: str) -> int | None:
    numbers = [int(m.group(1)) for m in _BUILDING_NUMBER_RE.finditer(text)]
    return min(numbers) if numbers else None


def timeline_coverage(
    members: Sequence[tuple[Site, Sequence[TimelineRow]]],
    today: date,
    timeline: TimelineOverride | None = None,
) -> Coverage:
    """How far the timelines of one campus's sites date the campus (see Coverage). A cited
    `timeline` entry decides first. Every site's notes count; the cited sources and the name only
    of the first site, since the others are its phases and their sources describe them as such."""
    if timeline is not None:
        if timeline.coverage == "whole":
            return Coverage(evidence=timeline.quote, cited=True)
        return Coverage(partial=True, evidence=timeline.quote, cited=True)

    def evidence(row: TimelineRow) -> str:
        return f"{row.day.isoformat()}: {event_note(row_text(row)) or ''}".rstrip()

    primary = members[0][0]
    titles = [t for t, _ in selected_links(primary.selected_sources)]
    actuals = [[r for r in rows if r.day <= today] for _site, rows in members]
    for actual in actuals:
        if actual and row_operational(actual[0]):
            return Coverage(partial=True, evidence=evidence(actual[0]))
        for i, r in enumerate(actual):
            text = row_text(r)
            if _PARTIAL_RE.search(text) or (i == 0 and _PARTIAL_FIRST_RE.search(text)):
                return Coverage(partial=True, evidence=evidence(r))
    for title in titles:
        if _PARTIAL_RE.search(title):
            return Coverage(partial=True, evidence=title)
    for title in (primary.name, *titles):
        if _EXPANSION_RE.search(title):
            return Coverage(
                held=True, evidence=title, why="a cited source or the name mentions an expansion"
            )
    part = "the name or the first row names part of a site"
    if _PART_NAME_RE.search(primary.name):
        return Coverage(held=True, evidence=primary.name, why=part)
    first = actuals[0][0] if actuals[0] else None
    if first is not None:
        text = row_text(first)
        lowest = _first_building_number(text)
        if (_PART_BUILDINGS_RE.search(text) and lowest is None) or (lowest or 1) > 1:
            return Coverage(held=True, evidence=evidence(first), why=part)
    for site, rows in members:
        texts = [
            *(f"{k}: {v}" for k, v in (("Name", site.name), ("Owner", site.owner))),
            *(f"{k}: {v}" for k, v in (("Users", site.users), ("Project", site.project))),
            *(t for t, _ in selected_links(site.selected_sources)),
            *(evidence(r) for r in rows if r.day <= today),
        ]
        for text in texts:
            m = _MINER_RE.search(strip_links(strip_invisible(text)))
            if m:
                return Coverage(
                    held=True,
                    evidence=clean_text(text)[:NOTE_MAX],
                    why=f"the site is a bitcoin miner's ({m.group(0)})",
                )
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
    name: str,
    ov: LocationOverride,
    *,
    counties: CountyIndex,
    gazetteer: Gazetteer,
    postal_city: str | None = None,
) -> Placed:
    """The location an override describes, checked like `atlas validate` rule 5 would; a point
    never lies outside the county the entry names, at any precision (as in atlas.geocode).

    A county entry names no city or municipality, since the county's point need not lie in it
    (AWS Berwick's Luzerne County point is in Rice township, its campus in Salem Township). A
    municipality (a township, a New England town) is placed at its county subdivision's Gazetteer
    point in the entry's county, at `locality`. A city that is also postal_city, the city of
    Epoch's address, counts only with place_check: the postal city is only near the site."""
    from atlas.geocode import (
        COUNTY_CHECKED_PRECISIONS,
        POINT_IN_POLYGON_PRECISIONS,
        normalize_place,
    )

    def bad(why: str) -> FetchError:
        return epoch_error(f"config/overrides/epoch.json entry {name!r}: {why}")

    county = counties.get(ov.county_fips) if ov.county_fips else None
    if ov.county_fips and (county is None or county.state_abbr != ov.state_abbr):
        raise bad(f"county_fips {ov.county_fips} is not a county of {ov.state_abbr}")
    if ov.precision == "county" and (ov.city or ov.municipality):
        raise bad(
            "a county entry names no city or municipality: the county's point need not lie in "
            "it (give precision locality to place the site at the municipality)"
        )
    if (
        ov.precision == "locality"
        and ov.city
        and postal_city
        and normalize_place(ov.city) == normalize_place(postal_city)
        and ov.place_check is None
    ):
        raise bad(
            f"{ov.city!r} is also the postal city of Epoch's address, which is only near the site "
            "(07 §6.5): say in place_check how the site was found inside the city's Census place "
            "polygon, or place it at the county a source states"
        )
    lat, lon = ov.lat, ov.lon
    if (lat is None) != (lon is None):
        raise bad("set both lat and lon, or neither")
    method: str | None = "manual"
    what = f"({lat}, {lon})"
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
            what = f"the Gazetteer place {ov.city!r}"
        elif ov.precision == "locality" and ov.municipality:
            if county is None:
                raise bad(
                    "a municipality needs county_fips: the same name is used in many counties"
                )
            sub = gazetteer.cousub_entry(ov.state_abbr, ov.municipality, county.fips)
            if sub is None:
                raise bad(
                    f"no Gazetteer county subdivision {ov.municipality!r} in county {county.fips}"
                )
            lat, lon = sub.lat, sub.lon
            method = "gazetteer"
            what = f"the Gazetteer county subdivision {ov.municipality!r}"
        elif ov.precision in ("state", "unknown"):
            method = None
        elif ov.precision == "county":
            raise bad("precision county needs county_fips (or lat and lon)")
        elif ov.precision == "locality":
            raise bad("precision locality needs city or municipality (or lat and lon)")
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
            raise bad(f"{what} is not inside county {county.fips}")
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


def _cite(
    sources: list[dict[str, Any]],
    cit: Citation,
    supports: Sequence[str],
    *,
    published_at: datetime | None = None,
) -> str:
    """The id of the source for cit: a source the record already has for its URL (the location
    source, a Selected Source), which then also supports `supports` and takes the citation's quote
    when it has none, else a new source."""
    url = str(HttpUrl(str(cit.source_url)))
    for source in sources:
        if str(HttpUrl(str(source["url"]))) == url:
            if source.get("quote") is None:
                source.update(
                    quote=cit.quote,
                    quote_match="human",
                    retrieved_at=cit.retrieved_at.isoformat(),
                    archive_url=str(cit.archive_url) if cit.archive_url else None,
                    publisher=cit.publisher or source["publisher"],
                    source_type=cit.source_type or source["source_type"],
                )
            source["supports"] = _unique([*source["supports"], *supports])
            if published_at is not None and source.get("published_at") is None:
                source["published_at"] = published_at.isoformat()
            return str(source["id"])
    sid = f"s{len(sources) + 1}"
    sources.append(
        {
            "id": sid,
            "url": str(cit.source_url),
            "archive_url": str(cit.archive_url) if cit.archive_url else None,
            "publisher": cit.publisher or host_of(str(cit.source_url)),
            "title": None,
            "source_type": cit.source_type or classify_source(str(cit.source_url)),
            "published_at": published_at.isoformat() if published_at is not None else None,
            "retrieved_at": cit.retrieved_at.isoformat(),
            "quote": cit.quote,
            "quote_match": "human",
            "supports": list(supports),
        }
    )
    return sid


def _first_report_event(
    report: FirstReport, events: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """A first_reported event for a cited report, when it is earlier than every event dated
    today or earlier and its status is not ahead of the first of them (07 §2.3); else None."""
    actual = [e for e in events if not e["planned"]]
    if not actual:
        return None
    first = min(actual, key=lambda e: period_start(FuzzyDate.model_validate(e["as_of"])))
    if report.day >= period_start(FuzzyDate.model_validate(first["as_of"])):
        return None
    if first["status"] in ACTIVE_ORDER and ACTIVE_ORDER.index(report.status) > ACTIVE_ORDER.index(
        first["status"]
    ):
        return None
    return {
        "status": report.status,
        "event": "first_reported",
        "as_of": {"value": report.day.isoformat(), "precision": "day"},
        "phase_id": None,
        "planned": False,
        "note": "The earliest dated report among the record's sources",
    }


def campus_record(
    members: Sequence[tuple[Site, Sequence[TimelineRow]]],
    placed: Placed,
    *,
    ctx: ImportContext,
    retrieved_at: str,
    coverage: Coverage | None = None,
    override: SiteOverride | None = None,
) -> FacilityRecord:
    """The candidate record for one campus: one Epoch site, or several at one street address
    (the first names the record; each is a phase). id PLACEHOLDER_ID, rollup applied.

    A timeline that does not date the facility (coverage.observed) becomes `other` events on a
    phase for the tracked buildings, which carries the capacity; the record then has no capacity,
    water use or cost of its own, and no construction or operating date. The override of the
    campus's first site adds, each with its cited source: the source of a `timeline` entry
    (supporting /phases); a `facility_status` (for an observed timeline only) as an `other` event
    on a phase of the buildings Epoch does not track; and a `first_report` (for a timeline that
    dates the facility, when it is the earliest) as the record's first_reported event, after which
    no Epoch row is a first report.
    """
    timeline = override.timeline if override is not None else None
    if coverage is None:
        coverage = timeline_coverage(members, ctx.today, timeline)
    observed = coverage.observed
    phased = observed or len(members) > 1
    primary = members[0][0]
    facility_status = override.facility_status if override is not None and observed else None
    report = override.first_report if override is not None and not observed else None

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

    if timeline is not None and coverage.cited:
        _cite(sources, timeline, ["/phases"] if observed else ["/status_history"])

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
            events.append(
                {**e, "seq": (period_start(FuzzyDate.model_validate(e["as_of"])), order, e["seq"])}
            )
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
    if facility_status is not None:
        sid = _cite(sources, facility_status, ["/status_history"])
        pid = f"{phase_slug(primary.name)}-untracked"
        phases.append(
            {
                "phase_id": pid,
                "name": UNTRACKED_PHASE_NAME.format(name=primary.name),
                "capacity": {},
                "source_ids": [sid],
            }
        )
        day = facility_status.as_of
        events.append(
            {
                "seq": (day, -1, 0),
                "status": facility_status.status,
                "event": "other",
                "as_of": {"value": day.isoformat(), "precision": "day"},
                "phase_id": pid,
                "planned": day > ctx.today,
                "source_ids": [sid],
                "note": f"The facility's status, from {facility_status.publisher or 'a cited source'}",
            }
        )
        operational = operational or facility_status.status == "operating"
    reported = _first_report_event(report, events) if report is not None else None
    if reported is not None and report is not None:
        published = report.published_at if isinstance(report.published_at, datetime) else None
        sid = _cite(sources, report, ["/status_history"], published_at=published)
        # The report is the first one: no Epoch row is a first report any more.
        for e in events:
            if e["event"] == "first_reported":
                e["event"] = "other"
        events.append({**reported, "seq": (report.day, -1, 0), "source_ids": [sid]})
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
    record: FacilityRecord, coverage: Coverage, *, cited_status: bool = False
) -> tuple[str, dict[str, Any], bool] | None:
    """The review item (reason, data, keep) a campus's coverage calls for, if any; keep is False
    when the record is held back.

    A held site: Epoch's fields do not say whether its timeline is the facility's, so the dates
    and capacity it would give the facility are left out (the record is kept). A partial timeline
    whose tracked buildings are not operating: the facility (older, or with a building Epoch does
    not count) may be further along, so the tracked buildings' status is not the facility's and
    the record is held back, unless a cited facility_status gives it.
    """
    data: dict[str, Any] = {"evidence": coverage.evidence, "status": record.status}
    if coverage.held:
        return (
            f"{coverage.why or 'Epoch fields suggest it'}, so Epoch's timeline may cover only "
            "buildings added to or converted from an older facility: the record gives no "
            "construction or operating date and puts the capacity on the phase; restore them if "
            "the timeline covers the whole facility (a cited timeline entry with coverage whole "
            "in config/overrides/epoch.json)",
            data,
            True,
        )
    if coverage.partial and record.status != "operating" and not cited_status:
        return (
            "Epoch's timeline covers only part of the facility and the buildings it tracks are "
            f"{record.status}, so the facility may be further along and its status is unknown: no "
            "record until a cited facility_status entry in config/overrides/epoch.json gives it",
            data,
            False,
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
        # places and a postal city's point; Epoch states no town or township, so it needs the
        # county subdivisions (downloaded on first use) only for an override that names one.
        places = ctx.places() if census is not None else None
        townships = any(o.location and o.location.municipality for o in overrides.values())
        gazetteer = ctx.gazetteer() if townships else Gazetteer.load()
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
            site = next(
                (s for s, _ in members if (o := overrides.get(s.name)) and o.location),
                members[0][0],
            )
            try:
                placed = self._place(site, overrides, census, gazetteer, counties, places)
            except GeocoderError as e:
                raise epoch_error(f"Census Geocoder: {e}") from e
            if placed is None:
                for s, _ in members:
                    unplaced(s)
                continue
            methods[placed.method] += 1
            primary = members[0][0].name
            override = overrides.get(primary)
            coverage = timeline_coverage(
                members, ctx.today, override.timeline if override is not None else None
            )
            try:
                record = campus_record(
                    members,
                    placed,
                    ctx=ctx,
                    retrieved_at=retrieved_at,
                    coverage=coverage,
                    override=override,
                )
            except (ValidationError, RollupError) as e:
                flag("invalid", primary, "the row does not map to a valid record", error=str(e))
                continue
            cited_status = (
                coverage.observed and override is not None and override.facility_status is not None
            )
            item = coverage_review(record, coverage, cited_status=cited_status)
            if item is not None:
                flag("conflict", primary, item[0], **item[1])
            if item is not None and not item[2]:
                coverages["status_unknown"] += 1
                continue
            candidates.append(Candidate(tuple(s.name for s, _ in members), record))
            coverages["held" if coverage.held else "partial" if coverage.partial else "whole"] += 1
            if record.dates.get("first_reported") is not None and any(
                e.event == "first_reported" and e.source_ids != ["s1"]
                for e in record.status_history
            ):
                coverages["first_reports"] += 1
            if cited_status:
                coverages["facility_statuses"] += 1

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
            "timelines_status_unknown": coverages["status_unknown"],
            "first_reports_cited": coverages["first_reports"],
            "facility_statuses_cited": coverages["facility_statuses"],
            "missing_location": sum(1 for i in review if i.kind == "missing_location"),
            "geocode_failed": sum(1 for i in review if i.kind == "geocode_failed"),
        }
        names = {clean_text(r["Name"]) for r in site_rows}  # every country: an entry is per name
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
        overrides: Mapping[str, SiteOverride],
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
        parsed = parse_address(site.address) if site.address else None
        if override is not None and override.location is not None:
            return place_override(
                site.name,
                override.location,
                counties=counties,
                gazetteer=gazetteer,
                postal_city=parsed.city if parsed is not None else None,
            )
        if parsed is None:
            return None
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
