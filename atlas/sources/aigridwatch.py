"""AI GridWatch data center project tracker: a pipeline seed (07 §4.1, §4.3).

Reads https://aigridwatch.com/data/projects.json (CC BY 4.0; the license field is checked). Each
verified project becomes a `project` record at `locality` precision:

- the point is AI GridWatch's approximate locality coordinate, checked to lie in the state and in
  the county the locality names; a point outside that county is not used, and the row is placed
  like a row without coordinates (Gazetteer place in the county, else the county's point);
- the county comes from the locality text ("Muncy Township (Lycoming County)", "Caddo Parish");
- status_history is built from the milestone dates (announced, rezoning filed, a hearing a
  decision on its day or an event of its day says was held, decision), plus a first_reported
  event at the earliest dated entry of the row's event log. A date on the 1st of a month is read
  at month precision. The derived `stage` goes through the status crosswalk (07 §4.7). When the
  milestones do not roll up to the stage's status, an `other` event records the stage as of the
  row's as_of date; a row with no as_of has no date for it and is held for review, and so is a row
  whose event log reports an approval or a groundbreaking the stage has not reached;
- size_mw keeps its figure in mw_as_stated and becomes it_mw or utility_request_mw only when the
  row's note states that basis (07 §2.4); a row that is a power supply deal or a generation
  facility is out of scope (07 §2.2);
- rows marked verified=false are leads, not facts, and become `unverified_upstream` review items;
- AI GridWatch republishes most of Epoch AI's US sites, and has rows of its own for some of them
  under other names. A row that is the same site as a stored Epoch record becomes a
  `possible_duplicate` item instead of a second record (EpochSites: the same name or id in the
  same state, a `source` that only that record cites, or, in the record's county, its own source
  being a link specific to that site or its street address in its name). A row tied only through
  its event log, or whose MW differ from the site's by more than x2, one that cites an Epoch AI
  page, one that only shares an organization with Epoch sites within 5 km, and one the rules tie
  to several sites are held for review without naming one. The run refuses a store without Epoch
  records unless --without-epoch is given, since `epoch` must run first;
- a row whose id begins with another row's name (AI GridWatch shifted the ids of a block of PA DEP
  rows) is held for review, since records are matched on that id;
- a reviewer releases a held row through config/overrides/aigridwatch.json (one entry per row
  id, with the checks released, a reason and the review date);
- parties hold organizations only (07 §5.3): a person, a capacity figure ("67 MW") or a
  parenthetical note ("(parcels)", "(AWS)") is never stored as a name.

The per-project event log (events[]) is not imported in M1: only its dates, kinds and the few
markers above are read.

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
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, TypeAdapter, ValidationError, model_validator

from atlas.crosswalk import UnknownStatus, from_aigridwatch_stage
from atlas.geo.fips import IN_SCOPE, state_by_abbr
from atlas.schema.record import (
    PLACEHOLDER_ID,
    AtlasModel,
    EventType,
    FacilityRecord,
    FuzzyDate,
    SourceType,
    Status,
    StatusEvent,
    StatusReason,
)
from atlas.schema.rollup import (
    ACTIVE_ORDER,
    RollupError,
    apply_rollup,
    event_key,
    mw_display,
    period_start,
)
from atlas.sources.base import Candidate, ImportResult, ReviewItem, load_input
from atlas.text import clean_text as _clean_text
from atlas.text import find_personal_data

if TYPE_CHECKING:
    from atlas.geo.counties import County, CountyIndex
    from atlas.geocode import Gazetteer, GeocodeResult
    from atlas.net import FetchError, FetchResult
    from atlas.sources.base import ImportContext

PROJECTS_URL = "https://aigridwatch.com/data/projects.json"
OVERRIDES_PATH = Path("config/overrides/aigridwatch.json")
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
# A link ties a row to an Epoch site only when their MW agree within this factor (07 §6.6, Size).
MW_FACTOR = 2.0
# Precisions at which a point says where the site is, not only its county or state.
_PRECISE = frozenset({"footprint", "parcel", "site", "address", "street", "locality"})
_WORD_RE = re.compile(r"[^\W_]+")

_OUTCOMES: dict[str, tuple[Status, EventType]] = {
    "approved": ("permitted", "approved"),
    "denied": ("denied", "denied"),
    "withdrawn": ("cancelled", "withdrawn"),
    "moratorium": ("paused", "paused"),
}
_FILING_EVENT_KINDS = frozenset({"filing", "rezoning"})
# A past hearing counts as held when an event of its day, of one of these kinds, says it was held
# or voted, and nothing in it says the hearing was scheduled, moved or continued: AI GridWatch's
# hearing_date is "the next/decisive public hearing", and hearings get moved.
_HEARING_EVENT_KINDS = frozenset({"hearing", "meeting", "vote"})
_HEARING_HELD_RE = re.compile(r"\b(?:held|voted)\b", re.I)
_HEARING_NOT_HELD_RE = re.compile(
    r"\b(?:schedul\w*|will|would|to be|anticipat\w*|expect\w*|set for|slated|postpon\w*|"
    r"continu\w*|reschedul\w*|cancel+(?:ed|led)?|moved|delay\w*|upcoming|planned)\b",
    re.I,
)
# The earliest entry of the event log dates first_reported, at announced (or proposed for the
# row's own filings), but not for a site AI GridWatch first lists as built or being built.
_REPORT_PROPOSED_KINDS = frozenset({"filing", "rezoning", "permit"})
_REPORT_SKIPPED_KINDS = frozenset({"construction", "operational"})
_REPORTED_STATUSES = frozenset(
    {"announced", "proposed", "permitted", "paused", "denied", "cancelled"}
)
# An entry reports on the project when it names a data center (or the row's name), and is not
# about the place's rules or the land's history.
_REPORT_SUBJECT_RE = re.compile(r"\bdata[- ]?cent(?:er|re)s?\b", re.I)
_REPORT_CONTEXT_RE = re.compile(
    r"\b(?:ordinances?|moratori\w*|regulations?|zoning code|zoning changes|text amendment|"
    r"annex\w*|registered|was formed|interconnection|paused?)\b",
    re.I,
)
# Event-log markers of a later milestone than the stage: an approval of the project's land-use
# application, or a groundbreaking. Words close before a marker that put it in the future, deny it
# or make it conditional void it.
_LOG_HEDGE_RE = re.compile(
    r"\b(?:expect\w*|anticipat\w*|plan(?:s|ned)?|could|would|will|may|might|if|once|seek\w*|"
    r"pending|recommend\w*|not|never|no|schedul\w*|set to|slated|intends?|aims?|asked|before|"
    r"until|whether)\b|n't\b",
    re.I,
)
# The decision's object, and the words between the verb and it that make it another decision ("a
# resolution raising conditional use fees", "the petition was rejected and the rezoning remained").
_LOG_OBJECT = (
    r"(?:\W+(?!(?:resolutions?|ordinances?|fees?|moratori\w*|amendments?|text|bans?|appeals?|"
    r"motions?|and|but|while|so|as)\b)[\w'-]+){0,6}?\W+(?:rezoning|conditional[- ]use|"
    r"special[- ](?:use|exception)|site[- ]plan|development plan|planned unit development|PUD)\b"
)
_LOG_APPROVAL_RE = re.compile(
    r"\b(?:voted(?:\s+[\w-]+){0,2}\s+to\s+approve|approved|approves|approving|granted|upheld)"
    + _LOG_OBJECT,
    re.I,
)
_LOG_DENIAL_RE = re.compile(
    r"\b(?:voted(?:\s+[\w-]+){0,2}\s+to\s+(?:deny|reject)|denied|denies|rejected|rejects)"
    + _LOG_OBJECT,
    re.I,
)
_LOG_GROUNDBREAKING_RE = re.compile(
    r"\b(?:groundbreaking|ground-breaking|broke ground|breaks ground|broken ground)\b", re.I
)
# A groundbreaking in a clause about the power supply is not the data center's.
_LOG_ENERGY_RE = re.compile(
    r"\b(?:power plants?|power station|generating station|gas plants?|substations?|transmission|"
    r"solar|battery|pipeline|turbines?)\b",
    re.I,
)
_CLAUSE_RE = re.compile(r"(?<=[.;])\s+")
# Withdrawn: the developer withdrew only when the text names the developer, the applicant or a
# party of the row as the one who withdrew; a court ruling that ended the project is litigation.
_WITHDREW_RE = r"\b(?:withdr[ae]w\w*|withdrawn|pull(?:ed|s)|dropped)\b"
_COURT_RULING_RE = re.compile(
    r"\b(?:judicially\s+(?:voided|vacated|overturned)|court\s+(?:ruled|ruling|voided|vacated|"
    r"overturned|struck)|(?:voided|vacated|overturned|struck down)\s+by\s+(?:a|the)\s+"
    r"(?:\w+\s+){0,3}(?:court|judge))\b",
    re.I,
)
_WITHDRAWAL_EVENT_KINDS = frozenset({"withdrawal", "withdrawn"})
# A row that is a power supply deal or a generation facility, not a data center site (07 §2.2).
_NOT_A_DATA_CENTER_RE = re.compile(
    r"\b(?:power purchase agreement|PPA|solar farm|solar project|power generation facility|"
    r"not a data cent(?:er|re))\b",
    re.I,
)
# size_mw and its basis: the note's figure equal to size_mw, and the words around it.
_FIGURE_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\+?\s*(MW|megawatts?|GW|gigawatts?)(?!\w)",
    re.I,
)
_IT_AFTER_RE = re.compile(r"^\W{0,3}(?:of\s+)?(?:[Cc]ritical(?:\s+IT)?|IT)\b")
_POWER_AFTER_RE = re.compile(
    r"^\W{0,3}(?:(?!(?:with|and|plus|including|for|to|in|on|at)\b)[\w-]+\s+){0,3}?"
    r"(?:power plants?|plants?|solar|nuclear|reactors?|turbines?|"
    r"generation|output|substations?|fuel[- ]cells?|batter(?:y|ies)|gas[- ]fired)\b",
    re.I,
)
_PHASE_BEFORE_RE = re.compile(r"\bphase\s*(?:\d+|[IVX]+|one)\b[^.;]{0,20}$", re.I)
_PHASE_AFTER_RE = re.compile(r"^\W{0,3}phase\b", re.I)
_SUPPLY_BEFORE_RE = re.compile(
    r"\b(?:supply|supplies|supplied|contract(?:ed|s)?|commit(?:ted|s)?|secur(?:es|ed)|"
    r"seek(?:s|ing)?|request(?:s|ed|ing)?|interconnection|grid connection)\b[^.;]*$",
    re.I,
)
_SUPPLY_AFTER_RE = re.compile(
    r"^\W{0,3}(?:of\s+)?(?:contracted|committed|requested|interconnection|grid capacity|"
    r"load request)\b",
    re.I,
)
SizeBasis = Literal["it", "utility_request", "power_source", "phase"]
# Ids: a generic name is told apart by its operator ("KSR" + "Unnamed Data Center").
_GENERIC_NAME_RE = re.compile(r"^unnamed data cent(?:er|re)s?$", re.I)
# Organization names compared without an alias table: suffixes dropped, then letters and digits.
_ORG_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|lp|llp|plc|"
    r"holdings?|group)\b\.?",
    re.I,
)


def agw_error(message: str) -> FetchError:
    """The AI GridWatch file cannot be used (license or layout). A FetchError, so `atlas import`
    reports it and exits 1; atlas.net (httpx) loads only here."""
    from atlas.net import FetchError

    return FetchError(message)


# ---------------------------------------------------------------------------- small parsers


def clean_text(value: object) -> str:
    return _clean_text(str(value or ""))


def parse_day(value: object) -> date | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class AgwDate:
    """An AI GridWatch date. One on the 1st of a month is that month: AI GridWatch writes
    month-level dates as YYYY-MM-01 (on 2026-10-08, 9 of 37 announced dates, 5 of 34 filing and 5
    of 39 decision dates fall on the 1st, against 0 of 31 hearing and 0 of 232 as_of dates)."""

    start: date  # the day, or the first day of the month
    precision: Literal["day", "month"]

    @property
    def end(self) -> date:
        if self.precision == "day":
            return self.start
        year, month = divmod(self.start.month, 12)
        return date(self.start.year + year, month + 1, 1) - timedelta(days=1)

    def fuzzy(self) -> dict[str, str]:
        value = self.start.isoformat()
        return {
            "value": value if self.precision == "day" else value[:7],
            "precision": self.precision,
        }


def parse_agw_date(value: object) -> AgwDate | None:
    day = parse_day(value)
    if day is None:
        return None
    return AgwDate(day, "month" if day.day == 1 else "day")


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


def named_counties(loc: Locality, state: str, counties: CountyIndex) -> list[County]:
    """The counties the locality names that the county file knows, in the order named."""
    found: dict[str, County] = {}
    for name in loc.county_texts:
        c = counties.by_name(state, name)
        if c is not None:
            found.setdefault(c.fips, c)
    return list(found.values())


def resolve_county(
    loc: Locality,
    state: str,
    counties: CountyIndex,
    point: tuple[float, float] | None,
) -> County | None:
    """The named county that contains the point (with the validate tolerance; of two that do, near
    their border, the one the point lies in); without a point, the named county when there is one.
    Else None: a record never names a county its point is outside of."""
    found = named_counties(loc, state, counties)
    if point is None:
        return found[0] if len(found) == 1 else None
    inside = [c for c in found if counties.contains(c.fips, point[0], point[1])]
    if len(inside) > 1:
        exact = counties.lookup(point[0], point[1])
        inside = [c for c in inside if exact is not None and c.fips == exact.fips]
    return inside[0] if len(inside) == 1 else None


# ---------------------------------------------------------------------------- status


def row_events(project: dict[str, Any]) -> list[dict[str, Any]]:
    events = project.get("events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def late_announcement(project: dict[str, Any]) -> tuple[date, str] | None:
    """(announced, the earlier milestone) when AI GridWatch dates the announcement after a filing,
    hearing or decision: announcing then would be a backward move (07 §2.3)."""
    announced = parse_agw_date(project.get("announced"))
    if announced is None:
        return None
    earlier = [
        (day.start, key)
        for key in ("rezoning_filed", "hearing_date", "decided_date")
        if (day := parse_agw_date(project.get(key))) is not None and day.end < announced.start
    ]
    return (announced.start, min(earlier)[1]) if earlier else None


def hearing_held(project: dict[str, Any], hearing: AgwDate) -> bool:
    """Whether a milestone or an event says the hearing on that day took place: a decision dated
    that day, or an event of that day (hearing, meeting or vote, with a source) that says it was
    held or voted and does not say it was scheduled, moved or continued. A past hearing_date alone
    does not: AI GridWatch's field is the next or decisive hearing, and hearings get moved."""
    if hearing.precision != "day":
        return False
    outcome = clean_text(project.get("outcome")).casefold()
    if outcome in _OUTCOMES and parse_day(project.get("decided_date")) == hearing.start:
        return True
    for e in row_events(project):
        if parse_day(e.get("date")) != hearing.start or not clean_text(e.get("source")):
            continue
        summary = clean_text(e.get("summary"))
        if (
            clean_text(e.get("kind")).casefold() in _HEARING_EVENT_KINDS
            and _HEARING_HELD_RE.search(summary)
            and not _HEARING_NOT_HELD_RE.search(summary)
        ):
            return True
    return False


def unconfirmed_hearing(project: dict[str, Any], today: date) -> AgwDate | None:
    """A hearing_date on or before today that nothing in the row says was held (hearing_held)."""
    hearing = parse_agw_date(project.get("hearing_date"))
    if hearing is None or hearing.start > today or hearing_held(project, hearing):
        return None
    return hearing


def milestone_events(project: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Events from the milestone dates, in date order; dates after today are planned, a date on
    the 1st of a month is that month. An announcement dated after a later-stage milestone is left
    out (late_announcement), and so is a past hearing nothing says was held (hearing_held)."""
    found: list[tuple[AgwDate, int, Status, EventType]] = []
    announced = parse_agw_date(project.get("announced"))
    if announced and late_announcement(project) is None:
        found.append((announced, 0, "announced", "announced"))
    filed = parse_agw_date(project.get("rezoning_filed"))
    if filed:
        found.append((filed, 1, "proposed", "application_filed"))
    hearing = parse_agw_date(project.get("hearing_date"))
    if hearing and hearing.start > today:
        found.append((hearing, 2, "proposed", "hearing_scheduled"))
    elif hearing and hearing_held(project, hearing):
        found.append((hearing, 2, "proposed", "hearing_held"))
    decided = parse_agw_date(project.get("decided_date"))
    outcome = clean_text(project.get("outcome")).casefold()
    if decided and outcome in _OUTCOMES:
        status, event = _OUTCOMES[outcome]
        found.append((decided, 3, status, event))
    found.sort(key=lambda t: (t[0].start, t[1]))
    return [
        {
            "seq": i + 1,
            "status": status,
            "event": event,
            "as_of": day.fuzzy(),
            "planned": day.start > today,
            "source_ids": ["s1"],
        }
        for i, (day, _, status, event) in enumerate(found)
    ]


def _about_the_project(project: dict[str, Any], summary: str) -> bool:
    """Whether an event-log entry reports on the project: it names a data center or the row's
    name, and is not about the place's rules or the land's history (an ordinance, a moratorium or
    other regulations, an annexation, an LLC's registration, an interconnection request). A
    keyword test, so it errs toward leaving an entry out: a later first report, never one dated by
    a county's data center ordinance."""
    name = clean_text(project.get("name"))
    m = _PAREN_RE.match(name)
    name = (m.group("outside") if m else name).casefold()
    named = _REPORT_SUBJECT_RE.search(summary) or (len(name) > 3 and name in summary.casefold())
    return bool(named) and not _REPORT_CONTEXT_RE.search(summary)


def first_report(
    project: dict[str, Any], status: Status, events: list[dict[str, Any]], today: date
) -> dict[str, Any] | None:
    """A first_reported event at the earliest dated entry of the row's event log that reports on
    the project (_about_the_project), for a row without an announced date, when that entry is
    earlier than every milestone: otherwise a row with only a decision or a hearing date would be
    first reported on that date. The log often starts with the site's history (a property sale,
    a county ordinance, the applicant LLC's registration), and those entries do not date it. A
    row with an announced date ("ISO date first publicly proposed") is first reported then. The
    event's status is announced (proposed when the entry is one of the row's own filings), so it
    never sets the current status. There is none when the stage is built or being built (an early
    entry may describe the site as it stands), when the entry is a construction or operation
    report, or when it would make a backward move."""
    if status not in _REPORTED_STATUSES or any(e["event"] == "announced" for e in events):
        return None
    dated = sorted(
        (
            (day, clean_text(e.get("kind")).casefold())
            for e in row_events(project)
            if (day := parse_agw_date(e.get("date"))) is not None
            and day.start <= today
            and _about_the_project(project, clean_text(e.get("summary")))
        ),
        key=lambda t: t[0].start,
    )
    if not dated:
        return None
    day, kind = dated[0]
    if kind in _REPORT_SKIPPED_KINDS:
        return None
    actual = [e for e in events if not e["planned"]]
    if actual and day.start >= period_start(FuzzyDate.model_validate(actual[0]["as_of"])):
        return None
    reported: Status = "proposed" if kind in _REPORT_PROPOSED_KINDS else "announced"
    if reported == "proposed" and any(e["status"] == "announced" for e in actual):
        return None
    return {
        "seq": 0,
        "status": reported,
        "event": "first_reported",
        "as_of": day.fuzzy(),
        "planned": False,
        "source_ids": ["s1"],
        "note": f"Earliest dated report in AI GridWatch's event log ({kind[:40] or 'no kind'})",
    }


@dataclass(frozen=True)
class LogMilestone:
    """A milestone the row's event log reports that the stage has not reached."""

    status: Status
    what: str  # "an approval of its land-use application", "a groundbreaking", ...
    day: AgwDate
    kind: str
    source: str | None
    detail: str = ""  # appended to the reason (", with no filing after it")


def _unhedged(text: str, start: int, end: int, after: int = 0) -> bool:
    return not _LOG_HEDGE_RE.search(text[max(0, start - 40) : end + after])


def log_milestone(project: dict[str, Any], status: Status, today: date) -> LogMilestone | None:
    """A milestone the event log reports that an active stage has not reached: a denial of the
    project's land-use application with no filing after it (Forsyth County "voted 5-2 to deny the
    rezoning request" while the stage still read Awaiting decision), else the most advanced of an
    approval of its rezoning, conditional or special use, special exception, site plan,
    development plan or PUD ("Person County commissioners voted to approve the rezoning") and a
    groundbreaking that is not the power supply's. The kinds alone do not say it (a `vote` may go
    either way, a `construction` entry may be another site's), so only these phrases count, and a
    word close before them that defers, conditions or denies them voids them."""
    if status not in ACTIVE_ORDER:
        return None
    events = [(parse_agw_date(e.get("date")), e) for e in row_events(project)]
    best: LogMilestone | None = None
    for day, e in events:
        if day is None or day.start > today:
            continue
        source = clean_text(e.get("source")) or None
        refiled = any(
            later is not None
            and later.start > day.start
            and clean_text(other.get("kind")).casefold() in _FILING_EVENT_KINDS
            for later, other in events
        )
        for clause in _CLAUSE_RE.split(clean_text(e.get("summary"))):
            if not refiled and any(
                _unhedged(clause, m.start(), m.start()) for m in _LOG_DENIAL_RE.finditer(clause)
            ):
                what = "a denial of its land-use application"
                kind = clean_text(e.get("kind"))
                return LogMilestone("denied", what, day, kind, source, ", and no filing after it")
            found: list[tuple[Status, str]] = []
            if any(
                _unhedged(clause, m.start(), m.start()) for m in _LOG_APPROVAL_RE.finditer(clause)
            ):
                found.append(("permitted", "an approval of its land-use application"))
            if not _LOG_ENERGY_RE.search(clause) and any(
                _unhedged(clause, m.start(), m.end(), 20)
                for m in _LOG_GROUNDBREAKING_RE.finditer(clause)
            ):
                found.append(("under_construction", "a groundbreaking"))
            for reported, what in found:
                rank = ACTIVE_ORDER.index(reported)
                if rank > ACTIVE_ORDER.index(status) and (
                    best is None or rank > ACTIVE_ORDER.index(best.status)
                ):
                    best = LogMilestone(reported, what, day, clean_text(e.get("kind")), source)
    return best


def withdrawal_reason(project: dict[str, Any]) -> StatusReason | None:
    """Why a Withdrawn row ended, from its note and its withdrawal entries: litigation when they
    cite a court ruling, developer_withdrawal when they name the developer, the applicant or a
    party of the row as the one who withdrew; else none (a mayor dropping his support, a lapsed
    agreement or a landowner backing out is not the developer's withdrawal)."""
    texts = [clean_text(project.get("note"))] + [
        clean_text(e.get("summary"))
        for e in row_events(project)
        if clean_text(e.get("kind")).casefold() in _WITHDRAWAL_EVENT_KINDS
    ]
    if any(_COURT_RULING_RE.search(t) for t in texts):
        return "litigation"
    names: set[str] = set()
    for role in ("operator", "owner", "filing_llc"):
        for part in split_names(clean_text(project.get(role)), r"\s+/\s+|\s*;\s*"):
            m = _TRAILING_NOTE_RE.match(part)
            # Only matched against the row's own text, never stored: a person's name may stay.
            for name in (m.group("name"), m.group("note")) if m else (part,):
                if name:
                    names.add(name)
                    first = name.split()[0]
                    if len(first) >= 3 and first[0].isupper():
                        names.add(first)
    who = "|".join(
        [r"developers?", r"applicants?", r"petitioners?", r"the company"]
        + [re.escape(n) for n in sorted(names, key=len, reverse=True)]
    )
    pattern = re.compile(rf"\b(?:{who})\b[^.;]{{0,60}}?{_WITHDREW_RE}", re.I)
    if any(pattern.search(t) for t in texts):
        return "developer_withdrawal"
    return None


def not_a_data_center(project: dict[str, Any]) -> str | None:
    """The phrase that makes the row a power supply deal or a generation facility rather than a
    data center site ("power purchase agreement", "solar farm", "not a data center"), if any."""
    for key in ("name", "note"):
        m = _NOT_A_DATA_CENTER_RE.search(clean_text(project.get(key)))
        if m:
            return m.group(0)
    return None


def size_basis(size: float, note: str) -> SizeBasis | None:
    """What the row's note says size_mw measures: critical IT load ("up to 300MW critical IT"), a
    supply or interconnection ("Talen agreed to supply 960 MW", "3.2 GW contracted"), a power
    source's capacity ("835MW nuclear output", "450 MW gas-fired power plant") or one phase's
    ("phase 1 is 800MW"). None when the note does not give the figure or says nothing about it:
    AI GridWatch's schema calls the field "Planned IT/critical load", but its rows carry other
    bases, so the schema text alone is not the basis (07 §2.4)."""
    for clause in _CLAUSE_RE.split(clean_text(note)):
        for m in _FIGURE_RE.finditer(clause):
            unit = 1000.0 if m.group(2).casefold().startswith("g") else 1.0
            if abs(float(m.group(1).replace(",", "")) * unit - size) > 0.5:
                continue
            before, after = (
                clause[max(0, m.start() - 30) : m.start()],
                clause[m.end() : m.end() + 40],
            )
            if _IT_AFTER_RE.match(after):
                return "it"
            if _POWER_AFTER_RE.match(after):
                return "power_source"
            if _SUPPLY_BEFORE_RE.search(before):
                return "utility_request"
            if _PHASE_BEFORE_RE.search(before) or _PHASE_AFTER_RE.match(after):
                return "phase"
            if _SUPPLY_AFTER_RE.match(after):
                return "utility_request"
            return None
    return None


def has_filing(project: dict[str, Any]) -> bool:
    if parse_day(project.get("rezoning_filed")):
        return True
    return any(
        clean_text(e.get("kind")).casefold() in _FILING_EVENT_KINDS for e in row_events(project)
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


def id_stems(project: dict[str, Any]) -> tuple[str, ...]:
    """The forms a row's id starts with: its name in id form, and for a generic name ("Unnamed
    Data Center") its operator's and the name's ("ksr-unnamed-data-center")."""
    name = clean_text(project.get("name"))
    stems = [name_slug(name)]
    operator = clean_text(project.get("operator"))
    if _GENERIC_NAME_RE.match(name) and operator:
        stems.append(f"{name_slug(operator)}-{name_slug(name)}")
    return tuple(s for s in stems if s)


def _starts_with(pid: str, stem: str) -> bool:
    return pid == stem or pid.startswith(stem + "-")


def foreign_ids(projects: list[Any]) -> dict[str, str]:
    """Rows whose id starts with another row's name (in the same state) and not with their own:
    {row id: the id of the row it names}. On 2026-10-08 one block of PA DEP rows had each row's id
    on the next row ("zediker-station-…" holding PECO's Limerick project, the Zediker Station row
    under "highlands-west-…"). Records are matched on the id, so such a row is held: imported
    under the id, it would name another project and churn once AI GridWatch fixes the ids. A stem
    of one word ("aws") is too common to say whose id it is."""
    rows: list[tuple[str, str, tuple[str, ...]]] = []
    by_state: dict[str, list[tuple[str, str]]] = {}
    for project in projects:
        if not isinstance(project, dict) or not (pid := clean_text(project.get("id"))):
            continue
        state = clean_text(project.get("state")).upper()
        stems = id_stems(project)
        rows.append((pid, state, stems))
        by_state.setdefault(state, []).extend((s, pid) for s in stems if "-" in s)
    out: dict[str, str] = {}
    for pid, state, stems in rows:
        if any(_starts_with(pid, s) for s in stems):
            continue
        other = next(
            (o for s, o in by_state.get(state, []) if o != pid and _starts_with(pid, s)), None
        )
        if other is not None:
            out[pid] = other
    return out


def org_key(name: str) -> str:
    """An organization name for comparison: accents, corporate suffixes and everything but
    letters and digits dropped ("X.AI Corp" -> "xai", "SpaceXAI" -> "spacexai")."""
    folded = unicodedata.normalize("NFKD", clean_text(name))
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", _ORG_SUFFIX_RE.sub(" ", plain).casefold())


def orgs_overlap(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two sets of org_key names may share an organization: the same key, or one key of
    three or more characters inside the other (xAI in SpaceXAI, QTS in QTS Data Centers). With
    data/orgs.json empty in M1 there is no alias table, and the rules that read this only hold a
    row for review, so a near match counts rather than a merge on a name that differs."""
    return any(
        x == y or (min(len(x), len(y)) >= 3 and (x in y or y in x)) for x in a for y in b if x and y
    )


def record_orgs(record: FacilityRecord) -> frozenset[str]:
    parties = record.parties
    return frozenset(
        k for o in (*parties.operator, *parties.owner, *parties.tenant) if (k := org_key(o.name))
    )


@dataclass(frozen=True)
class Twin:
    """How an AI GridWatch row is the same site as a stored Epoch AI record.

    record_id is the Epoch record. It is None for "epoch_source", and for "weak_link", "nearby"
    and "several", where candidates lists the Epoch records the row may be: those rows are held
    for review, not merged."""

    how: str
    record_id: str | None
    candidates: tuple[str, ...] = ()
    evidence: str | None = None  # the link or street that tied the row to the record
    detail: str | None = None  # why a link is too weak to merge on ("weak_link")


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
    orgs: frozenset[str]  # owner, operator and tenant names, as org_key
    street: tuple[str, ...]  # house number and street words (street_tokens), () when none
    mw: float | None = None  # IT MW, else facility MW (mw_display)


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
                orgs=record_orgs(r),
                street=(house, *street_words) if house and street_words else (),
                mw=mw_display(r.capacity)[0],
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

    def _cites(self, rid: str, key: str | None) -> bool:
        """Whether the Epoch record, or an AI GridWatch copy of its site, cites the link."""
        if key is None:
            return False
        if any(r == rid for _, r in self.by_url.get(key, [])):
            return True
        return any(self.copies.get(other) == rid for other in self.rows_by_link.get(key, set()))

    def _weak_link(self, project: dict[str, Any], rid: str, evidence: str) -> str | None:
        """Why a link that ties the row to one site is too weak to merge on, if it is: the tie is
        only in the row's event log and the row's own source is cited by neither the Epoch record
        nor its copy (AI GridWatch copies a campus's history into rows about other projects: its
        Amazon Northern Indiana expansion carries the New Carlisle stories), or the two MW differ
        by more than x2 (07 §6.6, Size)."""
        source = clean_text(project.get("source"))
        key = _url_key(source) if source else None
        if evidence != key and not self._cites(rid, key):
            return "the link is only in the row's event log, and its own source is not the site's"
        site = self.sites.get(rid)
        size = parse_number(project.get("size_mw"))
        mw = site.mw if site is not None else None
        if mw and size and size > 0 and max(size, mw) / min(size, mw) > MW_FACTOR:
            return f"its {size:g} MW and the site's {mw:g} MW differ by more than x2"
        return None

    def _local_orgs(self, record: FacilityRecord, here: str | None) -> set[str]:
        """The Epoch sites in the county (`here`) that may share an organization with the row."""
        orgs = record_orgs(record)
        return {
            s.record_id
            for s in self.sites.values()
            if s.state == record.location.state_abbr
            and here is not None
            and s.county_fips == here
            and orgs_overlap(s.orgs, orgs)
        }

    def _nearby(self, record: FacilityRecord) -> tuple[str, ...]:
        """Epoch sites within 5 km that may share an organization, both points at locality
        precision or finer."""
        from atlas.geocode import distance_m

        loc = record.location
        orgs = record_orgs(record)
        if loc.precision not in _PRECISE or loc.lat is None or loc.lon is None or not orgs:
            return ()
        return tuple(
            sorted(
                s.record_id
                for s in self.sites.values()
                if s.state == loc.state_abbr
                and s.precise
                and orgs_overlap(s.orgs, orgs)
                and s.lat is not None
                and s.lon is not None
                and distance_m(loc.lat, loc.lon, s.lat, s.lon) <= NEARBY_M
            )
        )

    def twin(self, project: dict[str, Any], record: FacilityRecord) -> Twin | None:
        """The location rules, for a row that `match` does not tie to Epoch and that became
        `record`. The row lies in an Epoch site's county and either cites a link specific to that
        site ("link") or gives its street address in its name or id ("street").

        A link merges only when it is the row's own source (or the row's source is one the site
        or its copy cites) and the MW agree within x2; otherwise the row is held for review
        ("weak_link", the site in candidates). The row is also held, not merged ("several": the
        record ids are in candidates), when the rules point to more than one site, or when it may
        also share an organization with another Epoch site in that county (AI GridWatch sometimes
        lists a company's campuses in a county as one row; names are compared with orgs_overlap,
        so xAI and SpaceXAI count as one); and ("nearby") when it only may share an organization
        with Epoch sites within 5 km, both points at locality precision or finer.
        """
        from atlas.geocode import street_tokens

        loc = record.location
        pid = clean_text(project.get("id"))
        here = loc.county_fips or self._county_at(loc.lat, loc.lon)
        source = clean_text(project.get("source"))
        source_key = _url_key(source) if source else None
        local = [s for s in self.sites.values() if s.state == loc.state_abbr]
        found: dict[str, tuple[str, str]] = {}
        if here is not None:
            for key in sorted(row_links(project)):
                rid = self._site_of_link(key, pid)
                site = self.sites.get(rid) if rid else None
                if (
                    site is not None
                    and site.state == loc.state_abbr
                    and site.county_fips == here
                    and (site.record_id not in found or key == source_key)
                ):
                    found[site.record_id] = ("link", key)
            words = " ".join(_WORD_RE.findall(f"{clean_text(project.get('name'))} {pid}"))
            tokens = street_tokens(words)
            for site in local:
                if site.street and site.county_fips == here and _has_run(tokens, site.street):
                    found.setdefault(site.record_id, ("street", " ".join(site.street)))
        if found:
            also = self._local_orgs(record, here)
            if len(found) == 1 and not also - set(found):
                ((rid, (how, evidence)),) = found.items()
                weak = self._weak_link(project, rid, evidence) if how == "link" else None
                if weak is not None:
                    return Twin("weak_link", None, (rid,), evidence=evidence, detail=weak)
                return Twin(how, rid, evidence=evidence)
            return Twin("several", None, tuple(sorted(set(found) | also)))
        near = self._nearby(record)
        return Twin("nearby", None, near) if near else None

    def candidates(self, project: dict[str, Any], record: FacilityRecord) -> tuple[str, ...]:
        """The Epoch records a row held for another reason (its source is an Epoch AI page) may
        be, for the reviewer: what the location rules tie it to, the sites in its county that may
        share an organization, and those within 5 km. Nothing is merged."""
        loc = record.location
        twin = self.twin(project, record)
        ids = set(twin.candidates) if twin else set()
        if twin is not None and twin.record_id is not None:
            ids.add(twin.record_id)
        ids |= self._local_orgs(record, loc.county_fips or self._county_at(loc.lat, loc.lon))
        ids |= set(self._nearby(record))
        return tuple(sorted(ids))

    def names(self, record_ids: tuple[str, ...]) -> str:
        """The records as "AWS Berwick (gwa-…)", for a review reason."""
        return ", ".join(
            f"{self.sites[r].name} ({r})" if r in self.sites else r for r in record_ids
        )


# ---------------------------------------------------------------------------- reviewer releases


ReleaseCheck = Literal["epoch", "stage", "id", "scope"]


class RowRelease(AtlasModel):
    """One entry of config/overrides/aigridwatch.json: a reviewer's decision that a check which
    holds an AI GridWatch row is wrong for it, so the row is imported.

    release names the checks: "epoch" (the Epoch AI site rules: the reviewer found the row is
    another site), "stage" (the event log's later milestone: the stage is right), "id" (the id
    that starts with another row's name) and "scope" (a power supply deal or generation facility).
    as_of is the day the reviewer confirmed the stage of a row that has no as_of, so its stage
    can be dated. reason says what the reviewer read; reviewed_at is when."""

    release: list[ReleaseCheck] = Field(default_factory=list)
    as_of: date | None = None
    reason: str = Field(min_length=1, max_length=300)
    reviewed_at: date

    @model_validator(mode="after")
    def _releases_something(self) -> RowRelease:
        if not self.release and self.as_of is None:
            raise ValueError("an entry releases at least one check or gives as_of")
        return self


_RELEASES = TypeAdapter(dict[str, RowRelease])


def load_releases(path: Path) -> dict[str, RowRelease]:
    """The reviewer releases. The default file may be absent (no release); a named one may not."""
    from atlas.geo.counties import resolve_reference

    resolved = resolve_reference(path)
    if path == OVERRIDES_PATH and not resolved.exists():
        return {}
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as e:
        raise agw_error(f"cannot read the AI GridWatch overrides file {resolved}: {e}") from e
    try:
        releases = _RELEASES.validate_json(text)
    except ValidationError as e:
        raise agw_error(f"{resolved} is not a valid AI GridWatch overrides file: {e}") from e
    for pid, entry in releases.items():
        if clean_text(pid) != pid or not pid or find_personal_data(entry.reason):
            raise agw_error(
                f"{resolved}: entry {pid!r} needs a plain row id and a reason without contact details"
            )
    return releases


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
        parser.add_argument(
            "--overrides",
            type=Path,
            default=OVERRIDES_PATH,
            help="reviewer releases of held rows (default: config/overrides/aigridwatch.json; "
            "none when that file is absent)",
        )

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        from atlas.geocode import Gazetteer

        releases = load_releases(getattr(args, "overrides", OVERRIDES_PATH))
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
        for pid, entry in releases.items():
            if entry.as_of is not None and entry.as_of > ctx.today:
                raise agw_error(
                    f"config/overrides/aigridwatch.json entry {pid!r}: as_of is after today"
                )

        gazetteer = Gazetteer.load()
        counties = ctx.counties()
        epoch_sites = EpochSites.from_records(ctx.records, counties)
        projects: list[Any] = doc["projects"]
        epoch_sites.add_rows(projects)
        foreign = foreign_ids(projects)
        row_ids = {clean_text(p.get("id")) for p in projects if isinstance(p, dict)}
        # AI GridWatch records already stored, not yet merged: one for a row now held is a twin.
        stored_agw = {
            v: rid
            for rid, r in ctx.records.items()
            if not r.merged_into
            for v in r.external_ids.get(self.match_key, [])
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

        def released(
            pid: str, release: RowRelease, check: str, kind: str, why: str, **data: Any
        ) -> None:
            """A check that would hold the row, released by a reviewer: noted, and imported."""
            stats["released"] += 1
            flag(
                kind,
                pid,
                f"{why}; released by config/overrides/aigridwatch.json (reviewed "
                f"{release.reviewed_at.isoformat()}: {release.reason}), so the row is imported",
                released=check,
                reviewed_at=release.reviewed_at.isoformat(),
                **data,
            )

        def epoch_why(twin: Twin) -> str:
            several = epoch_sites.names(twin.candidates)
            return {
                "name": "the same site as an Epoch AI record (same name and state)",
                "id": "the same site as an Epoch AI record (its id is that record's name, in "
                "the same state)",
                "source": "the same site as an Epoch AI record (it cites a link that record "
                "cites, in the same state)",
                "epoch_source": "AI GridWatch's source for this row is an Epoch AI page"
                + (f"; it may be Epoch AI's {several}" if twin.candidates else ""),
                "link": "the same site as an Epoch AI record (it lies in its county, and its own "
                "source is a link that site cites, or AI GridWatch's copy of it, and no other)",
                "street": "the same site as an Epoch AI record (it lies in its county and its "
                "name gives that record's street address)",
                "weak_link": f"may be Epoch AI's {several} (it lies in its county and cites a "
                f"link that only that site cites, but {twin.detail})",
                "nearby": f"may be the same site as Epoch AI's {several} (an organization in "
                "common, within 5 km)",
                "several": f"may be one or more of Epoch AI's {several} (a rule ties it to one "
                "of them, and another rule or an organization in common to another in the same "
                "county)",
            }[twin.how]

        def held(pid: str, project: dict[str, Any], twin: Twin) -> None:
            stats["epoch_duplicates"] += 1
            why = epoch_why(twin)
            if twin.candidates:
                stats["epoch_ambiguous"] += 1
                why += (
                    "; held for review, not merged: the row is not imported, and a reviewer "
                    "decides which site it is"
                )
            elif twin.record_id is None:
                why += "; held, not imported: a reviewer decides which Epoch AI site it is"
            elif twin.how in ("name", "id", "source") and "Epoch AI" in clean_text(
                project.get("note")
            ):
                why += (
                    "; AI GridWatch republishes this Epoch AI site, so the row is not imported a "
                    "second time"
                )
            else:
                why += (
                    "; AI GridWatch's own row for this Epoch site: not imported, and nothing from "
                    "it is merged into the Epoch record (its milestones wait for M4 entity "
                    "resolution)"
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

        def release_epoch(pid: str, release: RowRelease, twin: Twin) -> None:
            ids = [twin.record_id] if twin.record_id else list(twin.candidates)
            released(
                pid,
                release,
                "epoch",
                "possible_duplicate",
                epoch_why(twin),
                matched_by=twin.how,
                epoch_records=ids,
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
            release = releases.get(pid)
            free = set(release.release) if release else set()
            direct = epoch_sites.match(project, state)
            if direct is not None and direct[0] != "epoch_source":
                if release is None or "epoch" not in free:
                    held(pid, project, Twin(*direct))
                    continue
                release_epoch(pid, release, Twin(*direct))
            other = foreign.get(pid)
            if other is not None:
                why = (
                    f"the row's id starts with the name of AI GridWatch row {other!r}, not with "
                    "its own: AI GridWatch has given it another project's id"
                )
                if release is None or "id" not in free:
                    stats["held_for_review"] += 1
                    flag(
                        "conflict",
                        pid,
                        f"{why}. Records are matched on the id, so the row is held for review, "
                        "not imported, until the id is its own",
                        id_of=other,
                    )
                    continue
                released(pid, release, "id", "conflict", why, id_of=other)
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
                released=released,
                release=release,
                stats=row_stats,
            )
            if built is None:
                stats.update(row_stats)
                continue
            record, holds = built
            if direct is not None and direct[0] == "epoch_source":
                twin = Twin("epoch_source", None, epoch_sites.candidates(project, record))
                if release is None or "epoch" not in free:
                    del review[mark:]  # the row's own items: it is not imported
                    held(pid, project, twin)
                    continue
                release_epoch(pid, release, twin)
            elif direct is None:  # a released name, id or source match skips the location rules
                found = epoch_sites.twin(project, record)
                if found is not None and (release is None or "epoch" not in free):
                    del review[mark:]  # the row's own items: it is not imported
                    held(pid, project, found)
                    continue
                if found is not None and release is not None:
                    release_epoch(pid, release, found)
            if holds:
                stats["held_for_review"] += 1  # the row's items stay: they say why
                continue
            stats.update(row_stats)
            candidates.append(Candidate((pid,), record))

        planned = sum(1 for c in candidates for e in c.record.status_history if e.planned)
        metrics: dict[str, float | int] = {
            "projects": len(projects),
            "declared_count": int(doc["count"]) if isinstance(doc.get("count"), int) else -1,
            "unverified": stats["unverified"],
            "candidates": len(candidates),
            "planned_events": planned,
            "stage_events": stats["stage_events"],
            "first_reported_events": stats["first_reported_events"],
            "hearings_unconfirmed": stats["hearings_unconfirmed"],
            "located_source_coords": stats["source_coords"],
            "located_gazetteer": stats["gazetteer"],
            "located_county_centroid": stats["county_centroid"],
            "persons_dropped": stats["persons_dropped"],
            "out_of_scope": stats["out_of_scope"],
            "epoch_duplicates": stats["epoch_duplicates"],
            "epoch_ambiguous": stats["epoch_ambiguous"],
            "epoch_records_seen": len(epoch_sites.by_name),
            "held_for_review": stats["held_for_review"],
            "overrides_used": sum(1 for pid in releases if pid in row_ids),
            "overrides_unused": sum(1 for pid in releases if pid not in row_ids),
            "released": stats["released"],
            "upstream_events_unused": sum(
                len(p["events"])
                for p in projects
                if isinstance(p, dict) and isinstance(p.get("events"), list)
            ),
        }
        return ImportResult(self.name, [snapshot], candidates, review, metrics)

    @staticmethod
    def _place_by_text(
        loc: Locality, state: str, named: list[County], gazetteer: Gazetteer, counties: CountyIndex
    ) -> tuple[GeocodeResult, bool] | None:
        """A point from the locality text, for a row whose coordinates lie outside every county
        it names: the Gazetteer place when it lies in one of them, else the one named county's
        point on surface. With the bool: the place is only one the site is near."""
        from atlas.geocode import GeocodeRequest, geocode

        locality = next((t for t in (loc.place, loc.hint) if t and gazetteer.place(state, t)), None)
        nearby = locality is not None and locality == loc.hint and loc.hint_is_nearby
        for county in named if locality else []:
            req = GeocodeRequest(state_abbr=state, locality=locality, county_name=county.name)
            result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
            if result is not None and result.method == "gazetteer":
                return result, nearby
        if len(named) == 1:
            req = GeocodeRequest(state_abbr=state, county_name=named[0].name)
            result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
            return (result, False) if result is not None else None
        return None

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
        released: Callable[..., None],
        release: RowRelease | None,
        stats: Counter[str],
    ) -> tuple[FacilityRecord, tuple[str, ...]] | None:
        """The row's record, and the checks that hold it for review ("stage": the event log
        reports a later milestone; "as_of": the stage has no date). None when no record can be
        made; the review items say why."""
        from atlas.geocode import GeocodeRequest, geocode

        free = set(release.release) if release else set()
        holds: list[str] = []

        # Location ---------------------------------------------------------------------
        locality_text = clean_text(project["locality"])
        loc = parse_locality(locality_text, state)
        lat, lon = parse_number(project.get("lat")), parse_number(project.get("lon"))
        place_city = place_municipality = None
        if loc.place:
            if _MUNICIPALITY_RE.search(loc.place):
                place_municipality = loc.place
            else:
                place_city = loc.place
        location: dict[str, Any] | None = None
        result: GeocodeResult | None = None
        nearby = False
        if lat is not None and lon is not None:
            if not (18 <= lat <= 72 and -180 <= lon <= -64) or not counties.in_state(
                state, lat, lon
            ):
                flag(
                    "county_mismatch",
                    pid,
                    f"({lat}, {lon}) is not inside {state}",
                    locality=locality_text,
                )
                return None
            county = resolve_county(loc, state, counties, (lat, lon))
            named = named_counties(loc, state, counties)
            if county is None and named:
                # The locality text names the county, and the point is outside it: the text is
                # taken over the coordinates (EdgeCore's Louisa County campus had a point in
                # Goochland County), so no record shows a point known to be in the wrong county.
                names = " / ".join(f"{c.name} County" for c in named)
                placed = self._place_by_text(loc, state, named, gazetteer, counties)
                if placed is None:
                    flag(
                        "geocode_failed",
                        pid,
                        f"({lat}, {lon}) is not inside {names}, and neither a Gazetteer place in "
                        "it nor a single named county gives a point",
                        locality=locality_text,
                    )
                    return None
                result, nearby = placed
                placed_at = (
                    f"the Gazetteer place {result.city}"
                    if result.method == "gazetteer"
                    else f"the point of {names}"
                )
                flag(
                    "county_mismatch",
                    pid,
                    f"({lat}, {lon}) is not inside {names}: the coordinates are not used, and the "
                    f"record is placed at {placed_at} instead",
                    locality=locality_text,
                    lat=lat,
                    lon=lon,
                )
            else:
                if county is None and loc.county_texts:
                    flag(
                        "county_mismatch",
                        pid,
                        f"{' / '.join(loc.county_texts)} is not a county the county file knows; "
                        "the record keeps the point and names no county",
                        locality=locality_text,
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
                    locality=locality_text,
                )
                return None
        if location is None and result is not None:
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
        if location is None:
            return None

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

        # Scope ------------------------------------------------------------------------
        scope = "in_scope"
        phrase = not_a_data_center(project)
        if phrase is not None:
            why = (
                f"the row says {phrase!r}: a power supply deal or a generation facility, not a "
                "data center site (07 §2.2)"
            )
            if release is not None and "scope" in free:
                released(pid, release, "scope", "out_of_scope", why)
            else:
                scope = "out_of_scope"
                stats["out_of_scope"] += 1
                flag(
                    "out_of_scope",
                    pid,
                    f"{why}; the record is kept out of scope, so it is never published",
                    phrase=phrase,
                )

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
            # The figure always stays as stated; a basis only when the row's note gives one.
            capacity["mw_as_stated"] = f"AI GridWatch size_mw: {size_mw:g}"
            basis = size_basis(size_mw, clean_text(project.get("note")))
            if basis == "it":
                capacity["it_mw"] = size_mw
                field_meta["/capacity/it_mw"] = imported
            elif basis == "utility_request":
                capacity["utility_request_mw"] = size_mw
                field_meta["/capacity/utility_request_mw"] = imported
            elif basis is not None:
                what = {
                    "power_source": "a power source's capacity (a plant, a reactor or its output)",
                    "phase": "one phase's",
                }[basis]
                flag(
                    "unit_parse",
                    pid,
                    f"size_mw {size_mw:g} is {what}, by the row's note, not the data center's: "
                    "only mw_as_stated keeps it",
                    size_mw=f"{size_mw:g}",
                    basis=basis,
                )
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
        hearing = unconfirmed_hearing(project, ctx.today)
        if hearing is not None:
            stats["hearings_unconfirmed"] += 1
            flag(
                "unknown_status",
                pid,
                f"AI GridWatch's hearing_date ({hearing.fuzzy()['value']}) has passed, but no "
                "decision on that day and no entry of the row's event log for that day says the "
                "hearing was held (hearings get moved): no hearing event is imported",
                hearing_date=clean_text(project.get("hearing_date")),
            )
        first = first_report(project, status, events, ctx.today)
        if first is not None:
            events.insert(0, first)
            for i, e in enumerate(events):
                e["seq"] = i + 1
            stats["first_reported_events"] += 1
        log = log_milestone(project, status, ctx.today)
        if log is not None:
            why = (
                f"AI GridWatch's stage {stage!r} ({status}) is behind its own event log, which "
                f"reports {log.what} on {log.day.fuzzy()['value']} (event kind "
                f"{log.kind or 'blank'!r}){log.detail}"
            )
            evidence = {
                "stage": stage,
                "reported_status": log.status,
                "event_date": log.day.fuzzy()["value"],
                "event_kind": log.kind or None,
                "event_source": log.source,
            }
            if release is not None and "stage" in free:
                released(pid, release, "stage", "conflict", why, **evidence)
            else:
                holds.append("stage")
                flag(
                    "conflict",
                    pid,
                    f"{why}: the row is held for review, not imported, rather than published "
                    "with the stage's status",
                    **evidence,
                )
        if status == "cancelled" and status_reason is None:
            status_reason = withdrawal_reason(project)
        latest = _latest_actual([StatusEvent.model_validate(e) for e in events])
        if latest is None or latest.status != status:
            as_of_text = clean_text(project.get("as_of"))
            observed = parse_agw_date(as_of_text)
            note = f"AI GridWatch stage '{stage}' as of {as_of_text}"
            if observed is None and release is not None and release.as_of is not None:
                observed = AgwDate(release.as_of, "day")
                note = (
                    f"AI GridWatch stage '{stage}', confirmed by a reviewer on "
                    f"{release.as_of.isoformat()} (the row has no as_of)"
                )
            if observed is None:
                # The schema has no "status seen, date unknown", and the file's date is not the
                # day AI GridWatch read a source for the row: the stage cannot be dated.
                holds.append("as_of")
                flag(
                    "unknown_status",
                    pid,
                    f"AI GridWatch gives the row no as_of date, and no milestone reaches its "
                    f"stage {stage!r}: the stage has no date, so the row is held for review, not "
                    "imported (a reviewer who confirms the stage can date it in "
                    "config/overrides/aigridwatch.json)",
                    stage=stage,
                )
                observed = AgwDate(generated_day, "day")  # to check the row against Epoch only
            if latest is not None and observed.start < period_start(latest.as_of):
                observed = AgwDate(
                    period_start(latest.as_of),
                    "month" if latest.as_of.precision == "month" else "day",
                )
            events.append(
                {
                    "seq": len(events) + 1,
                    "status": status,
                    "event": "other",
                    "as_of": observed.fuzzy(),
                    "planned": False,
                    "source_ids": ["s1"],
                    "note": note,
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
            "scope": scope,
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
            return apply_rollup(FacilityRecord.model_validate(doc)), tuple(holds)
        except (ValidationError, RollupError) as e:
            flag("invalid", pid, "the row does not map to a valid record", error=str(e))
            return None


IMPORTER = AIGridWatchImporter()
