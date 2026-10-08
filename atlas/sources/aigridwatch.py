"""AI GridWatch data center project tracker: a pipeline seed (07 §4.1, §4.3).

Reads https://aigridwatch.com/data/projects.json (CC BY 4.0; the license field is checked). Each
verified project becomes a `project` record at `locality` precision:

- the point is AI GridWatch's approximate locality coordinate, checked to lie in the state; rows
  without coordinates are placed through atlas.geocode (Gazetteer place, else county by name);
- the county comes from the locality text ("Muncy Township (Lycoming County)", "Caddo Parish");
- status_history is built from the milestone dates (announced, rezoning filed, hearing, decision),
  and the derived `stage` goes through the status crosswalk (07 §4.7). When the milestones do not
  roll up to the stage's status, an `other` event records the stage as of the row's as_of date;
- rows marked verified=false are leads, not facts, and become `unverified_upstream` review items;
- AI GridWatch republishes most of Epoch AI's US sites, and has rows of its own for some of them
  under other names. A row that is the same site as a stored Epoch record becomes a
  `possible_duplicate` item instead of a second record (EpochSites: the same name or id in the
  same state, a `source` that only that record cites, an Epoch AI page as `source`, or, in the
  record's county, a link specific to that site or its street address). A row that only shares an
  organization with Epoch sites within 5 km, or that the rules tie to several sites, is held for
  review without naming one. The run refuses a store without Epoch records unless
  --without-epoch is given, since `epoch` must run first;
- parties hold organizations only (07 §5.3): a person, a capacity figure ("67 MW") or a
  parenthetical note ("(parcels)", "(AWS)") is never stored as a name.

The per-project event log (events[]) is not imported in M1.

Importing this module does no I/O.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from atlas.crosswalk import UnknownStatus, from_aigridwatch_stage
from atlas.geo.fips import IN_SCOPE, state_by_abbr
from atlas.schema.record import (
    PLACEHOLDER_ID,
    FacilityRecord,
    SourceType,
    Status,
    StatusEvent,
    StatusReason,
)
from atlas.schema.rollup import RollupError, apply_rollup, event_key, period_start
from atlas.sources.base import Candidate, ImportResult, ReviewItem, load_input
from atlas.text import find_personal_data, strip_invisible

if TYPE_CHECKING:
    from atlas.geo.counties import County, CountyIndex
    from atlas.geocode import Gazetteer
    from atlas.net import FetchError, FetchResult
    from atlas.sources.base import ImportContext

PROJECTS_URL = "https://aigridwatch.com/data/projects.json"
DATASET_URL = "https://aigridwatch.com/projects"
LICENSE = "CC-BY-4.0"
UPSTREAM_LICENSE = "CC BY 4.0"
CONFIDENCE = 0.70
MW_MAX = 10_000.0
ACRES_MAX = 100_000.0
S1_SUPPORTS = (
    "/canonical_name",
    "/aliases",
    "/parties",
    "/location",
    "/capacity",
    "/site",
    "/status_history",
)
REQUIRED_FIELDS = ("id", "name", "locality", "state", "stage", "verified")
# Hosts of agenda and code platforms count as government records, like .gov and .us hosts.
GOVERNMENT_HOST_MARKERS = ("legistar", "granicus", "civicplus", "municode")

_SPACE_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"^(?P<outside>.*?)\s*\((?P<inside>[^()]*)\)\s*$")
_COUNTY_RE = re.compile(r"^(?P<names>.+?)\s+(?P<kind>County|Counties|Parish|Parishes)$", re.I)
_AK_COUNTY_RE = re.compile(
    r"^(?P<names>.+?)\s+(?P<kind>City and Borough|Borough|Census Area|Municipality)$", re.I
)
_NAME_LIST_RE = re.compile(r"\s*/\s*|\s*,\s*|\s+and\s+|\s*&\s*")
_MUNICIPALITY_RE = re.compile(r"\b(?:Townships?|Boroughs?|Village)\b", re.I)
_HINT_NOISE_RE = re.compile(
    r"^(?:near|outside|north of|south of|east of|west of)\s+|\s+area$", re.I
)
_PERSON_ROLE_RE = re.compile(r"\((?:developer|owner|landowner|investor|individual)\)$", re.I)
_ORG_MARKER_RE = re.compile(
    r"\b(?:LLC|L\.L\.C\.|Inc|Corp|Corporation|Co|Company|Ltd|LP|LLP|Holdings|Group|Partners|"
    r"Development|Developers|Capital|Energy|Data|Fund|Trust|Properties|Ventures|Realty|"
    r"Center|Centre|Park|Campus|Project|Microgrids?|Solutions|Systems|Technologies|Infrastructure|"
    r"Power|Digital|Cloud|Mining|Industries|Investments?)\b\.?",
    re.I,
)
# A trailing parenthetical on a party: a role ("(parcels)"), an alias ("(AWS)") or a person's name.
_TRAILING_NOTE_RE = re.compile(r"^(?P<name>.*?\S)\s*\((?P<note>[^()]*)\)$")
# Two or three capitalized words ("Jane Example", "Jane Q. Example", "Jane O'Example").
_PERSON_NAME_RE = re.compile(
    r"^[A-Z][a-z]*(?:['\u2019-][A-Z]?[a-z]+)*\.?(?:\s+[A-Z][a-z]*(?:['\u2019-][A-Z]?[a-z]+)*\.?){1,2}$"
)
# A capacity typed into a party field ("67 MW").
_CAPACITY_RE = re.compile(r"^[\d.,]+\s*(?:MW|GW)$", re.I)
_EPOCH_HOST = "epoch.ai"
# An AI GridWatch row that shares an organization with an Epoch site this close is held for review.
NEARBY_M = 5_000.0
# Precisions at which a point says where the site is, not only its county or state.
_PRECISE = frozenset({"footprint", "parcel", "site", "address", "street", "locality"})
_WORD_RE = re.compile(r"[^\W_]+")

_MILESTONE_ORDER = {"announced": 0, "rezoning_filed": 1, "hearing_date": 2, "decided_date": 3}
_OUTCOMES: dict[str, tuple[str, str]] = {
    "approved": ("permitted", "approved"),
    "denied": ("denied", "denied"),
    "withdrawn": ("cancelled", "withdrawn"),
    "moratorium": ("paused", "paused"),
}
_FILING_EVENT_KINDS = frozenset({"filing", "rezoning"})


def agw_error(message: str) -> FetchError:
    """The AI GridWatch file cannot be used (license or layout). A FetchError, so `atlas import`
    reports it and exits 1; atlas.net (httpx) loads only here."""
    from atlas.net import FetchError

    return FetchError(message)


# ---------------------------------------------------------------------------- small parsers


def clean_text(value: object) -> str:
    return _SPACE_RE.sub(" ", strip_invisible(str(value or ""))).strip()


def parse_day(value: object) -> date | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_number(value: object) -> float | None:
    """size_mw and acres are int, float or ""; anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def looks_like_person(name: str) -> bool:
    """A party AI GridWatch annotates with a personal role ("X Y (developer)") and that carries no
    organization marker. Records hold organizations only (07 §5.3)."""
    return bool(_PERSON_ROLE_RE.search(name)) and not _ORG_MARKER_RE.search(
        _PERSON_ROLE_RE.sub("", name)
    )


def looks_like_person_name(text: str) -> bool:
    """Two or three capitalized words with no organization marker: a person's name."""
    text = clean_text(text)
    return bool(_PERSON_NAME_RE.match(text)) and not _ORG_MARKER_RE.search(text)


@dataclass(frozen=True)
class PartyName:
    """A party string reduced to an organization name (None when there is none)."""

    name: str | None
    person_dropped: bool = False  # a person's name was left out
    capacity: bool = False  # the string is a capacity figure, not a party


def party_name(text: str) -> PartyName:
    """The organization a party string names, without a trailing parenthetical note.

    "Example Holdings LLC (developer)" -> "Example Holdings LLC"; "Amazon (AWS)" -> "Amazon";
    "Example Ventures (Jane Example)" -> "Example Ventures", person dropped; "Jane Example
    (developer)" -> none, person dropped; "67 MW" -> none, a capacity.
    """
    text = clean_text(text).strip(" ;,")
    if _CAPACITY_RE.match(text):
        return PartyName(None, capacity=True)
    if looks_like_person(text) or find_personal_data(text):
        return PartyName(None, person_dropped=True)
    m = _TRAILING_NOTE_RE.match(text)
    if m is None:
        return PartyName(text or None)
    name = m.group("name").strip(" ;,")
    person = looks_like_person_name(m.group("note"))
    return PartyName(name or None, person_dropped=person)


def split_names(text: str, separators: str) -> list[str]:
    """Names split on the given regex, cleaned, de-duplicated, in order."""
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(separators, clean_text(text)):
        name = part.strip(" ;,")
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def filing_names(text: str) -> list[str]:
    """filing_llc split on " / " and ";"; a part that starts in lowercase is prose, not a name
    ("...LLC; applicant changed to ... in July 2025")."""
    return [n for n in split_names(text, r"\s+/\s+|\s*;\s*") if not n[0].islower()]


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def row_source_type(url: str) -> SourceType:
    host = host_of(url)
    if host.endswith((".gov", ".us")) or any(m in host for m in GOVERNMENT_HOST_MARKERS):
        return "government_record"
    return "news"


# ---------------------------------------------------------------------------- locality


@dataclass(frozen=True)
class Locality:
    """What the locality text says: a place (city or minor civil division), counties, a hint."""

    place: str | None  # the text outside the parentheses when it is not a county
    county_texts: tuple[str, ...]  # "Lycoming County", "Hays County", ...
    hint: str | None  # non-county text in parentheses ("Indianapolis", "Granbury")
    # The hint is a nearby place ("near Reno", "Granbury area"), not the one the site is in: it may
    # give a fallback point, never the city.
    hint_is_nearby: bool = False


def _county_names(text: str, state: str) -> list[str] | None:
    """["Chester County", "Montgomery County"] for "Chester / Montgomery County"; None when the
    text does not name counties. Boroughs and census areas are counties only in Alaska."""
    m = _COUNTY_RE.match(text) or (_AK_COUNTY_RE.match(text) if state == "AK" else None)
    if m is None:
        return None
    kind = m.group("kind")
    singular = {"counties": "County", "parishes": "Parish"}.get(kind.casefold(), kind)
    names = [n for n in _NAME_LIST_RE.split(m.group("names")) if n.strip()]
    return [f"{n.strip()} {singular}" for n in names]


def parse_locality(text: str, state: str) -> Locality:
    text = clean_text(text)
    outside, inside = text, ""
    m = _PAREN_RE.match(text)
    if m:
        outside, inside = m.group("outside").strip(), m.group("inside").strip()
    counties: list[str] = []
    places: list[str] = []
    for part in re.split(r"\s+/\s+", outside) if outside else []:
        names = _county_names(part, state)
        if names:
            counties.extend(names)
        elif part:
            places.append(part)
    hint: str | None = None
    nearby = False
    if inside:
        names = _county_names(inside, state)
        if names:
            counties.extend(names)
        else:
            first = inside.split(",", 1)[0]
            hint = _HINT_NOISE_RE.sub("", _HINT_NOISE_RE.sub("", first)).strip() or None
            nearby = bool(_HINT_NOISE_RE.search(first))
    place = " / ".join(places) or None
    if place and place.casefold().startswith("city of "):
        place = place[len("city of ") :].strip() or None
    return Locality(place, tuple(counties), hint, nearby)


def resolve_county(
    loc: Locality,
    state: str,
    counties: CountyIndex,
    point: tuple[float, float] | None,
) -> County | None:
    """The named county that contains the point (with the validate tolerance); without a point,
    the named county when there is one. Else None: a record never names a county its point is
    outside of."""
    found: dict[str, County] = {}
    for name in loc.county_texts:
        c = counties.by_name(state, name)
        if c is not None:
            found[c.fips] = c
    if point is None:
        return next(iter(found.values())) if len(found) == 1 else None
    inside = [c for c in found.values() if counties.contains(c.fips, point[0], point[1])]
    return inside[0] if len(inside) == 1 else None


# ---------------------------------------------------------------------------- status


def late_announcement(project: dict[str, Any]) -> tuple[date, str] | None:
    """(announced, the earlier milestone) when AI GridWatch dates the announcement after a filing,
    hearing or decision: announcing then would be a backward move (07 §2.3)."""
    announced = parse_day(project.get("announced"))
    if announced is None:
        return None
    earlier = [
        (day, key)
        for key in ("rezoning_filed", "hearing_date", "decided_date")
        if (day := parse_day(project.get(key))) is not None and day < announced
    ]
    return (announced, min(earlier)[1]) if earlier else None


def milestone_events(project: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Events from the milestone dates, in date order; dates after today are planned. An
    announcement dated after a later-stage milestone is left out (late_announcement)."""
    found: list[tuple[date, int, str, str]] = []
    announced = parse_day(project.get("announced"))
    if announced and late_announcement(project) is None:
        found.append((announced, 0, "announced", "announced"))
    filed = parse_day(project.get("rezoning_filed"))
    if filed:
        found.append((filed, 1, "proposed", "application_filed"))
    hearing = parse_day(project.get("hearing_date"))
    if hearing:
        kind = "hearing_held" if hearing <= today else "hearing_scheduled"
        found.append((hearing, 2, "proposed", kind))
    decided = parse_day(project.get("decided_date"))
    outcome = clean_text(project.get("outcome")).casefold()
    if decided and outcome in _OUTCOMES:
        status, event = _OUTCOMES[outcome]
        found.append((decided, 3, status, event))
    found.sort(key=lambda t: (t[0], t[1]))
    return [
        {
            "seq": i + 1,
            "status": status,
            "event": event,
            "as_of": {"value": day.isoformat(), "precision": "day"},
            "planned": day > today,
            "source_ids": ["s1"],
        }
        for i, (day, _, status, event) in enumerate(found)
    ]


def has_filing(project: dict[str, Any]) -> bool:
    if parse_day(project.get("rezoning_filed")):
        return True
    events = project.get("events")
    if not isinstance(events, list):
        return False
    return any(
        isinstance(e, dict) and clean_text(e.get("kind")).casefold() in _FILING_EVENT_KINDS
        for e in events
    )


def _latest_actual(events: list[StatusEvent]) -> StatusEvent | None:
    actual = [e for e in events if not e.planned]
    return max(actual, key=event_key) if actual else None


# ---------------------------------------------------------------------------- Epoch AI's sites


def _url_key(url: str) -> str | None:
    try:
        return str(HttpUrl(clean_text(url)))
    except ValidationError:
        return None


def row_links(project: dict[str, Any]) -> frozenset[str]:
    """The row's `source` and every event's `source`, normalized."""
    events = project.get("events")
    urls = [project.get("source")] + [
        e.get("source") for e in (events if isinstance(events, list) else []) if isinstance(e, dict)
    ]
    return frozenset(k for u in urls if (text := clean_text(u)) and (k := _url_key(text)))


def name_slug(text: str) -> str:
    """A name in AI GridWatch's id form: accents dropped, lower case, every other run of
    characters one "-" ("Microsoft-Nebius New Jersey" -> "microsoft-nebius-new-jersey")."""
    folded = unicodedata.normalize("NFKD", clean_text(text))
    plain = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def _has_run(tokens: tuple[str, ...], run: tuple[str, ...]) -> bool:
    n = len(run)
    return n > 0 and any(tokens[i : i + n] == run for i in range(len(tokens) - n + 1))


@dataclass(frozen=True)
class Twin:
    """How an AI GridWatch row is the same site as a stored Epoch AI record.

    record_id is the Epoch record (None for "epoch_source", and for "nearby" and "several",
    where candidates lists the Epoch records the row may be: those rows are held for review,
    not merged)."""

    how: str
    record_id: str | None
    candidates: tuple[str, ...] = ()
    evidence: str | None = None  # the link or street that tied the row to the record


@dataclass(frozen=True)
class EpochSite:
    """What the location rules read from one stored Epoch AI record."""

    record_id: str
    name: str  # the first epoch_name
    state: str
    county_fips: str | None  # the stated county, else the county containing the point
    lat: float | None
    lon: float | None
    precise: bool  # placed at locality precision or finer
    orgs: frozenset[str]  # owner, operator and tenant names, case-folded
    street: tuple[str, ...]  # house number and street words (street_tokens), () when none


@dataclass
class EpochSites:
    """The stored Epoch AI records, by state and name and by the links they cite, plus the AI
    GridWatch rows that copy them (see `twin`).

    AI GridWatch carries most of Epoch's US sites under Epoch's names, with `source` set to the
    first of Epoch's Selected Sources for the site (68 of the 69 same-name rows on 2026-10-08).
    For some of those sites it also has a row of its own under another name.
    """

    by_name: dict[tuple[str, str], str] = field(default_factory=dict)
    by_slug: dict[tuple[str, str], str] = field(default_factory=dict)
    # Each cited link (normalized) -> the (state, record id) of every Epoch record citing it.
    by_url: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    sites: dict[str, EpochSite] = field(default_factory=dict)
    counties: CountyIndex | None = None
    # AI GridWatch rows (add_rows): each link -> the ids of the rows citing it, and the rows that
    # are an Epoch site's copy (matched by name, id or source) -> that Epoch record.
    rows_by_link: dict[str, set[str]] = field(default_factory=dict)
    copies: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_records(
        cls, records: Mapping[str, FacilityRecord], counties: CountyIndex | None = None
    ) -> EpochSites:
        from atlas.geocode import house_number, street_name_tokens

        sites = cls(counties=counties)
        for rid, r in sorted(records.items()):
            names = r.external_ids.get("epoch_name") or []
            if not names or r.merged_into:
                continue
            state = r.location.state_abbr
            for n in names:
                sites.by_name.setdefault((state, clean_text(n).casefold()), rid)
                if slug := name_slug(n):
                    sites.by_slug.setdefault((state, slug), rid)
            for src in r.sources:
                key = _url_key(str(src.url)) if src.source_type != "open_dataset" else None
                cited = sites.by_url.setdefault(key, []) if key else None
                if cited is not None and (state, rid) not in cited:
                    cited.append((state, rid))
            loc = r.location
            house = house_number(loc.street)
            street_words = street_name_tokens(loc.street) if house else ()
            sites.sites[rid] = EpochSite(
                record_id=rid,
                name=clean_text(names[0]),
                state=state,
                county_fips=loc.county_fips or sites._county_at(loc.lat, loc.lon),
                lat=loc.lat,
                lon=loc.lon,
                precise=loc.precision in _PRECISE,
                orgs=frozenset(
                    clean_text(o.name).casefold()
                    for o in (*r.parties.owner, *r.parties.operator, *r.parties.tenant)
                ),
                street=(house, *street_words) if house and street_words else (),
            )
        return sites

    def _county_at(self, lat: float | None, lon: float | None) -> str | None:
        if self.counties is None or lat is None or lon is None:
            return None
        county = self.counties.lookup(lat, lon)
        return county.fips if county else None

    def match(self, project: dict[str, Any], state: str) -> tuple[str, str | None] | None:
        """(how, Epoch record id) when the row is the same site as an Epoch record: "name" (the
        same name in the same state), "id" (its id is an Epoch name in AI GridWatch's id form,
        with or without "-{state}", in the same state), "source" (its source is a link that
        exactly one Epoch record cites, in the same state; a report cited for several sites links
        none) or "epoch_source" (its source is an Epoch AI page; record id None)."""
        rid = self.by_name.get((state, clean_text(project.get("name")).casefold()))
        if rid is not None:
            return ("name", rid)
        pid = clean_text(project.get("id"))
        rid = self.by_slug.get((state, pid)) or self.by_slug.get(
            (state, pid.removesuffix(f"-{state.lower()}"))
        )
        if rid is not None:
            return ("id", rid)
        source = clean_text(project.get("source"))
        key = _url_key(source) if source else None
        cited = self.by_url.get(key, []) if key else []
        if len(cited) == 1 and cited[0][0] == state:
            return ("source", cited[0][1])
        host = host_of(source) if key else ""
        if host == _EPOCH_HOST or host.endswith("." + _EPOCH_HOST):
            return ("epoch_source", None)
        return None

    def add_rows(self, projects: list[Any]) -> None:
        """Index every row's links, and the verified rows that `match` ties to an Epoch record
        (AI GridWatch's copies of Epoch's sites, whose event logs AI GridWatch wrote itself)."""
        for project in projects:
            if not isinstance(project, dict) or not clean_text(project.get("id")):
                continue
            pid = clean_text(project["id"])
            for key in row_links(project):
                self.rows_by_link.setdefault(key, set()).add(pid)
            state = clean_text(project.get("state")).upper()
            if project.get("verified") is True and state in IN_SCOPE:
                direct = self.match(project, state)
                if direct is not None and direct[1] is not None:
                    self.copies[pid] = direct[1]

    def _site_of_link(self, key: str, pid: str) -> str | None:
        """The one Epoch site a link is specific to: cited by that site's record or by AI
        GridWatch's copy of it, by no other Epoch record, and by no other AI GridWatch row."""
        owners = {rid for _, rid in self.by_url.get(key, [])}
        for other in self.rows_by_link.get(key, set()) - {pid}:
            if other not in self.copies:
                return None
            owners.add(self.copies[other])
        return next(iter(owners)) if len(owners) == 1 else None

    def twin(self, project: dict[str, Any], record: FacilityRecord) -> Twin | None:
        """The location rules, for a row that `match` does not tie to Epoch and that became
        `record`. The row lies in an Epoch site's county and either cites a link specific to that
        site ("link") or gives its street address in its name or id ("street").

        Otherwise the row is held for review, not merged ("several": the record ids are in
        candidates), when the rules point to more than one site, or when it also shares an
        organization with another Epoch site in that county (AI GridWatch sometimes lists a
        company's campuses in a county as one row); and ("nearby") when it only shares an
        organization with Epoch sites within 5 km, both points at locality precision or finer.
        """
        from atlas.geocode import distance_m, street_tokens

        loc = record.location
        pid = clean_text(project.get("id"))
        here = loc.county_fips or self._county_at(loc.lat, loc.lon)
        orgs = {
            clean_text(o.name).casefold()
            for o in (*record.parties.operator, *record.parties.owner, *record.parties.tenant)
        }
        local = [s for s in self.sites.values() if s.state == loc.state_abbr]
        found: dict[str, tuple[str, str]] = {}
        if here is not None:
            for key in sorted(row_links(project)):
                rid = self._site_of_link(key, pid)
                site = self.sites.get(rid) if rid else None
                if site is not None and site.state == loc.state_abbr and site.county_fips == here:
                    found.setdefault(site.record_id, ("link", key))
            words = " ".join(_WORD_RE.findall(f"{clean_text(project.get('name'))} {pid}"))
            tokens = street_tokens(words)
            for site in local:
                if site.street and site.county_fips == here and _has_run(tokens, site.street):
                    found.setdefault(site.record_id, ("street", " ".join(site.street)))
        if found:
            also = {s.record_id for s in local if s.county_fips == here and s.orgs & orgs}
            if len(found) == 1 and not also - set(found):
                ((rid, (how, evidence)),) = found.items()
                return Twin(how, rid, evidence=evidence)
            return Twin("several", None, tuple(sorted(set(found) | also)))
        if loc.precision not in _PRECISE or loc.lat is None or loc.lon is None or not orgs:
            return None
        near = sorted(
            s.record_id
            for s in local
            if s.precise
            and s.orgs & orgs
            and s.lat is not None
            and s.lon is not None
            and distance_m(loc.lat, loc.lon, s.lat, s.lon) <= NEARBY_M
        )
        return Twin("nearby", None, tuple(near)) if near else None

    def names(self, record_ids: tuple[str, ...]) -> str:
        """The records as "AWS Berwick (gwa-…)", for a review reason."""
        return ", ".join(
            f"{self.sites[r].name} ({r})" if r in self.sites else r for r in record_ids
        )


# ---------------------------------------------------------------------------- the importer


class AIGridWatchImporter:
    name = "aigridwatch"
    match_key = "aigridwatch_id"
    owned_external_keys = ("aigridwatch_id",)
    review_sources = ("aigridwatch",)
    version = "1"
    help = "AI GridWatch project tracker (CC BY 4.0): contested proposals and their milestones"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--without-epoch",
            action="store_true",
            help="run on a store without Epoch AI records (only rows citing an Epoch AI page are "
            "held as Epoch's sites); without it the run refuses, since epoch must run first",
        )

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        from atlas.geocode import Gazetteer

        if not getattr(args, "without_epoch", False) and not any(
            r.external_ids.get("epoch_name") and not r.merged_into for r in ctx.records.values()
        ):
            raise agw_error(
                "the store holds no Epoch AI record. AI GridWatch republishes Epoch's sites, and "
                "only the Epoch records keep them from being imported twice: run `atlas import "
                "epoch` first, or pass --without-epoch"
            )

        def fetch_json() -> FetchResult:
            from atlas.net import fetch

            return fetch(
                ctx.http, PROJECTS_URL, allowed_types=("application/json",), max_bytes=20_000_000
            )

        data, snapshot = load_input(
            ctx, name=self.name, url=PROJECTS_URL, license=LICENSE, ext="json", fetch=fetch_json
        )
        try:
            doc = json.loads(data)
        except ValueError as e:
            raise agw_error(f"AI GridWatch projects.json is not JSON: {e}") from e
        if not isinstance(doc, dict) or not isinstance(doc.get("projects"), list):
            raise agw_error("AI GridWatch projects.json has no projects list; the layout changed")
        if doc.get("license") != UPSTREAM_LICENSE:
            raise agw_error(
                f"AI GridWatch license is {doc.get('license')!r}, not {UPSTREAM_LICENSE!r}; "
                "check the terms before importing"
            )
        generated = clean_text(doc.get("generated")) or None
        snapshot = snapshot.model_copy(update={"upstream_version": generated})
        generated_day = parse_day(generated) or ctx.today

        gazetteer = Gazetteer.load()
        counties = ctx.counties()
        epoch_sites = EpochSites.from_records(ctx.records, counties)
        projects: list[Any] = doc["projects"]
        epoch_sites.add_rows(projects)
        stored_agw = {
            v: rid for rid, r in ctx.records.items() for v in r.external_ids.get(self.match_key, [])
        }
        retrieved_at = snapshot.retrieved_at.isoformat()
        candidates: list[Candidate] = []
        review: list[ReviewItem] = []
        stats: Counter[str] = Counter()

        def flag(
            kind: str,
            external_id: str | None,
            reason: str,
            *,
            record_id: str | None = None,
            **data: Any,
        ) -> None:
            review.append(
                ReviewItem(
                    source=self.name,
                    kind=kind,
                    external_id=external_id,
                    record_id=record_id,
                    reason=reason,
                    data=data,
                )
            )

        def held(pid: str, project: dict[str, Any], twin: Twin) -> None:
            stats["epoch_duplicates"] += 1
            several = epoch_sites.names(twin.candidates)
            why = {
                "name": "the same site as an Epoch AI record (same name and state)",
                "id": "the same site as an Epoch AI record (its id is that record's name, in "
                "the same state)",
                "source": "the same site as an Epoch AI record (it cites a link that record "
                "cites, in the same state)",
                "epoch_source": "AI GridWatch's source for this row is an Epoch AI page",
                "link": "the same site as an Epoch AI record (it lies in its county and cites a "
                "link that only that site cites, in Epoch or in AI GridWatch's copy of it)",
                "street": "the same site as an Epoch AI record (it lies in its county and its "
                "name gives that record's street address)",
                "nearby": f"may be the same site as Epoch AI's {several} (an organization in "
                "common, within 5 km)",
                "several": f"may be one or more of Epoch AI's {several} (a rule ties it to one "
                "of them, and another rule or an organization in common to another in the same "
                "county)",
            }[twin.how]
            if twin.candidates:
                stats["epoch_ambiguous"] += 1
                why += (
                    "; held for review, not merged: the row is not imported, and a reviewer "
                    "decides which site it is"
                )
            else:
                why += (
                    "; AI GridWatch republishes Epoch's sites, so the row is not imported a "
                    "second time"
                )
            extra: dict[str, Any] = {}
            if twin.candidates:
                extra["epoch_records"] = list(twin.candidates)
            if twin.evidence:
                extra["evidence"] = twin.evidence
            stale = stored_agw.get(pid)
            if stale is not None:
                # merge_import files removed_upstream for it; this item says why.
                extra["stored_record"] = stale
                why += (
                    f". The store already holds AI GridWatch record {stale} for this row: it is "
                    "kept (records are never deleted) until a reviewer sets its merged_into"
                )
            flag(
                "possible_duplicate",
                pid,
                why,
                record_id=twin.record_id,
                matched_by=twin.how,
                source=clean_text(project.get("source")) or None,
                **extra,
            )

        for project in projects:
            if not isinstance(project, dict) or any(f not in project for f in REQUIRED_FIELDS):
                flag("invalid", None, "a project row lacks required fields")
                continue
            pid = clean_text(project["id"])
            if not pid:
                flag("invalid", None, "a project row has an empty id")
                continue
            if project.get("verified") is not True:
                stats["unverified"] += 1
                flag(
                    "unverified_upstream",
                    pid,
                    "AI GridWatch marks this row unverified: a lead to check, not a fact to cite",
                )
                continue
            state = clean_text(project["state"]).upper()
            if state_by_abbr(state) is None:
                flag("invalid", pid, f"unknown state {state!r}")
                continue
            if state not in IN_SCOPE:
                flag("out_of_scope", pid, f"{state} is outside the 50 states and DC")
                continue
            direct = epoch_sites.match(project, state)
            if direct is not None:
                held(pid, project, Twin(*direct))
                continue
            stage = clean_text(project["stage"])
            try:
                cw = from_aigridwatch_stage(stage, has_filing=has_filing(project))
            except UnknownStatus:
                flag("unknown_status", pid, f"unknown AI GridWatch stage {stage!r}", stage=stage)
                continue
            mark, row_stats = len(review), Counter[str]()
            built = self._record(
                project,
                pid=pid,
                state=state,
                stage=stage,
                status=cw.status,
                status_reason=cw.status_reason,
                rumor=cw.evidence_level == "rumor",
                ctx=ctx,
                generated_day=generated_day,
                retrieved_at=retrieved_at,
                gazetteer=gazetteer,
                counties=counties,
                flag=flag,
                stats=row_stats,
            )
            if built is None:
                stats.update(row_stats)
                continue
            twin = epoch_sites.twin(project, built)
            if twin is not None:
                del review[mark:]  # the row's own items: it is not imported
                held(pid, project, twin)
                continue
            stats.update(row_stats)
            candidates.append(Candidate((pid,), built))

        planned = sum(1 for c in candidates for e in c.record.status_history if e.planned)
        metrics: dict[str, float | int] = {
            "projects": len(projects),
            "declared_count": int(doc["count"]) if isinstance(doc.get("count"), int) else -1,
            "unverified": stats["unverified"],
            "candidates": len(candidates),
            "planned_events": planned,
            "stage_events": stats["stage_events"],
            "located_source_coords": stats["source_coords"],
            "located_gazetteer": stats["gazetteer"],
            "located_county_centroid": stats["county_centroid"],
            "persons_dropped": stats["persons_dropped"],
            "epoch_duplicates": stats["epoch_duplicates"],
            "epoch_ambiguous": stats["epoch_ambiguous"],
            "epoch_records_seen": len(epoch_sites.by_name),
            "upstream_events_unused": sum(
                len(p["events"])
                for p in projects
                if isinstance(p, dict) and isinstance(p.get("events"), list)
            ),
        }
        return ImportResult(self.name, [snapshot], candidates, review, metrics)

    def _record(
        self,
        project: dict[str, Any],
        *,
        pid: str,
        state: str,
        stage: str,
        status: Status,
        status_reason: StatusReason | None,
        rumor: bool,
        ctx: ImportContext,
        generated_day: date,
        retrieved_at: str,
        gazetteer: Gazetteer,
        counties: CountyIndex,
        flag: Callable[..., None],
        stats: Counter[str],
    ) -> FacilityRecord | None:
        from atlas.geocode import GeocodeRequest, geocode

        # Location ---------------------------------------------------------------------
        loc = parse_locality(clean_text(project["locality"]), state)
        lat, lon = parse_number(project.get("lat")), parse_number(project.get("lon"))
        place_city = place_municipality = None
        if loc.place:
            if _MUNICIPALITY_RE.search(loc.place):
                place_municipality = loc.place
            else:
                place_city = loc.place
        location: dict[str, Any]
        if lat is not None and lon is not None:
            if not (18 <= lat <= 72 and -180 <= lon <= -64) or not counties.in_state(
                state, lat, lon
            ):
                flag(
                    "county_mismatch",
                    pid,
                    f"({lat}, {lon}) is not inside {state}",
                    locality=clean_text(project["locality"]),
                )
                return None
            county = resolve_county(loc, state, counties, (lat, lon))
            if county is None and loc.county_texts:
                flag(
                    "county_mismatch",
                    pid,
                    f"({lat}, {lon}) is not inside {' / '.join(loc.county_texts)}; the record "
                    "keeps the point and names no county",
                    locality=clean_text(project["locality"]),
                )
            location = {
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "precision": "locality",
                "geocode_method": "source_coords",
                "city": place_city,
                "municipality": place_municipality,
                "county_name": county.name if county else None,
                "county_fips": county.fips if county else None,
                "state_abbr": state,
            }
            stats["source_coords"] += 1
        else:
            county = resolve_county(loc, state, counties, None)
            locality = next(
                (t for t in (loc.place, loc.hint) if t and gazetteer.place(state, t)), None
            )
            # A nearby place ("Storey County (near Reno)") may give the point, never the city.
            nearby = locality is not None and locality == loc.hint and loc.hint_is_nearby
            result = geocode(
                GeocodeRequest(
                    state_abbr=state,
                    locality=locality,
                    county_name=county.name if county else None,
                ),
                census=None,
                gazetteer=gazetteer,
                counties=counties,
            )
            if result is None:
                flag(
                    "geocode_failed",
                    pid,
                    "no coordinates, and neither a Gazetteer place nor a county matches "
                    "the locality",
                    locality=clean_text(project["locality"]),
                )
                return None
            location = {
                "lat": result.lat,
                "lon": result.lon,
                "precision": result.precision,
                "geocode_method": result.method,
                "city": place_city or (None if nearby else result.city),
                "municipality": place_municipality,
                "county_name": result.county_name,
                "county_fips": result.county_fips,
                "state_abbr": state,
            }
            stats[result.method or "none"] += 1

        # Parties and aliases ----------------------------------------------------------
        ref = {"source_ids": ["s1"]}

        def orgs(role: str, names: list[str]) -> list[str]:
            out: list[str] = []
            for text in names:
                party = party_name(text)
                if party.person_dropped:
                    stats["persons_dropped"] += 1
                if party.capacity:
                    flag(
                        "unit_parse",
                        pid,
                        f"{role} holds a capacity, not an organization; it is not imported",
                        **{role: text},
                    )
                if party.name and party.name.casefold() not in {n.casefold() for n in out}:
                    out.append(party.name)
            return out

        def field_names(role: str, separators: str) -> list[dict[str, Any]]:
            names = split_names(clean_text(project.get(role)), separators)
            return [{"name": n, **ref} for n in orgs(role, names)]

        filings = orgs("filing_llc", filing_names(clean_text(project.get("filing_llc"))))
        parties = {
            "operator": field_names("operator", r"\s+/\s+"),
            "owner": field_names("owner", r"\s+/\s+"),
            "tenant": field_names("tenant", r"\s+/\s+|\s*,\s+"),
            "filing_entities": [{"name": n, **ref} for n in filings],
        }
        aliases = [{"name": n, "kind": "filing_llc", **ref} for n in filings]

        # Capacity and site ------------------------------------------------------------
        imported = {"confidence": CONFIDENCE, "method": "imported", "source_ids": ["s1"]}
        field_meta: dict[str, Any] = {
            "/location": {
                "confidence": CONFIDENCE if location["geocode_method"] == "source_coords" else 0.60,
                "method": "imported"
                if location["geocode_method"] == "source_coords"
                else "derived",
                "source_ids": ["s1"],
            }
        }
        capacity: dict[str, Any] = {}
        site: dict[str, Any] = {}
        size_mw = parse_number(project.get("size_mw"))
        if size_mw is not None and 0 < size_mw <= MW_MAX:
            capacity["it_mw"] = size_mw
            field_meta["/capacity/it_mw"] = imported
        elif size_mw is not None or clean_text(project.get("size_mw")):
            flag(
                "unit_parse",
                pid,
                "size_mw is not a number in (0, 10000]",
                size_mw=str(project.get("size_mw")),
            )
        acres = parse_number(project.get("acres"))
        if acres is not None and 0 < acres <= ACRES_MAX:
            site["acreage"] = acres
            field_meta["/site/acreage"] = imported
        elif acres is not None or clean_text(project.get("acres")):
            flag(
                "unit_parse",
                pid,
                "acres is not a number in (0, 100000]",
                acres=str(project.get("acres")),
            )

        # Status history ---------------------------------------------------------------
        late = late_announcement(project)
        if late is not None:
            flag(
                "conflict",
                pid,
                f"AI GridWatch dates the announcement ({late[0].isoformat()}) after {late[1]} "
                f"({clean_text(project.get(late[1]))}); the announced milestone is left out, so "
                "the status history does not move backward",
                announced=late[0].isoformat(),
                **{late[1]: clean_text(project.get(late[1]))},
            )
        events = milestone_events(project, ctx.today)
        latest = _latest_actual([StatusEvent.model_validate(e) for e in events])
        if latest is None or latest.status != status:
            as_of_text = clean_text(project.get("as_of")) or generated_day.isoformat()
            observed = parse_day(as_of_text) or generated_day
            if latest is not None and observed < period_start(latest.as_of):
                observed = period_start(latest.as_of)
            events.append(
                {
                    "seq": len(events) + 1,
                    "status": status,
                    "event": "other",
                    "as_of": {"value": observed.isoformat(), "precision": "day"},
                    "planned": False,
                    "source_ids": ["s1"],
                    "note": f"AI GridWatch stage '{stage}' as of {as_of_text}",
                }
            )
            stats["stage_events"] += 1

        # Sources ----------------------------------------------------------------------
        sources: list[dict[str, Any]] = [
            {
                "id": "s1",
                "url": DATASET_URL,
                "publisher": "AI GridWatch",
                "title": "AI GridWatch data center project tracker",
                "source_type": "open_dataset",
                "license": LICENSE,
                "retrieved_at": retrieved_at,
                "supports": list(S1_SUPPORTS),
            }
        ]
        row_url = clean_text(project.get("source"))
        if row_url:
            try:
                HttpUrl(row_url)
            except ValidationError:
                row_url = ""
        if row_url:
            sources.append(
                {
                    "id": "s2",
                    "url": row_url,
                    "publisher": host_of(row_url),
                    "source_type": row_source_type(row_url),
                    "retrieved_at": retrieved_at,
                    "supports": [],
                }
            )

        name = clean_text(project["name"]) or pid
        where = location.get("city") or location.get("municipality")
        if not where and location.get("county_fips"):
            where = gazetteer.county_full_name(location["county_fips"])
        canonical = f"{name} ({where}, {state})" if where else f"{name} ({state})"
        ts = ctx.now.isoformat()
        doc: dict[str, Any] = {
            "id": PLACEHOLDER_ID,
            "record_type": "project",
            "canonical_name": canonical,
            "aliases": aliases,
            "parties": parties,
            "purpose": "unknown",
            "status": status,
            "status_reason": status_reason,
            "evidence_level": "rumor" if rumor else "reported",
            "status_history": events,
            "location": location,
            "capacity": capacity,
            "site": site,
            "external_ids": {"aigridwatch_id": [pid]},
            "sources": sources,
            "field_meta": field_meta,
            "review": {"state": "machine"},
            "created_at": ts,
            "updated_at": ts,
            "last_verified_at": ts,
        }
        try:
            return apply_rollup(FacilityRecord.model_validate(doc))
        except (ValidationError, RollupError) as e:
            flag("invalid", pid, "the row does not map to a valid record", error=str(e))
            return None


IMPORTER = AIGridWatchImporter()
