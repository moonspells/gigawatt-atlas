"""AI GridWatch data center project tracker: a pipeline seed (07 §4.1, §4.3).

Reads https://aigridwatch.com/data/projects.json (CC BY 4.0; the license field is checked). Each
verified project becomes a `project` record at `locality` precision:

- the point is AI GridWatch's approximate locality coordinate, checked to lie in the state; rows
  without coordinates are placed through atlas.geocode (Gazetteer place, else county by name);
- the county comes from the locality text ("Muncy Township (Lycoming County)", "Caddo Parish");
- status_history is built from the milestone dates (announced, rezoning filed, hearing, decision),
  and the derived `stage` goes through the status crosswalk (07 §4.7). When the milestones do not
  roll up to the stage's status, an `other` event records the stage as of the row's as_of date;
- rows marked verified=false are leads, not facts, and become `unverified_upstream` review items.

The per-project event log (events[]) is not imported in M1.

Importing this module does no I/O.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
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
    r"Development|Developers|Capital|Energy|Data|Fund|Trust|Properties|Ventures|Realty)\b\.?",
    re.I,
)

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
    if inside:
        names = _county_names(inside, state)
        if names:
            counties.extend(names)
        else:
            first = inside.split(",", 1)[0]
            hint = _HINT_NOISE_RE.sub("", _HINT_NOISE_RE.sub("", first)).strip() or None
    place = " / ".join(places) or None
    if place and place.casefold().startswith("city of "):
        place = place[len("city of ") :].strip() or None
    return Locality(place, tuple(counties), hint)


def resolve_county(
    loc: Locality,
    state: str,
    counties: CountyIndex,
    point: tuple[float, float] | None,
) -> County | None:
    """The named county; with several, the one containing the point; else None."""
    found: dict[str, County] = {}
    for name in loc.county_texts:
        c = counties.by_name(state, name)
        if c is not None:
            found[c.fips] = c
    if len(found) == 1:
        return next(iter(found.values()))
    if len(found) > 1 and point is not None:
        inside = [c for c in found.values() if counties.contains(c.fips, point[0], point[1])]
        if len(inside) == 1:
            return inside[0]
    return None


# ---------------------------------------------------------------------------- status


def milestone_events(project: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Events from the milestone dates, in date order; dates after today are planned."""
    found: list[tuple[date, int, str, str]] = []
    announced = parse_day(project.get("announced"))
    if announced:
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


# ---------------------------------------------------------------------------- the importer


class AIGridWatchImporter:
    name = "aigridwatch"
    match_key = "aigridwatch_id"
    owned_external_keys = ("aigridwatch_id",)
    review_sources = ("aigridwatch",)
    version = "1"
    help = "AI GridWatch project tracker (CC BY 4.0): contested proposals and their milestones"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        from atlas.geocode import Gazetteer

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
        retrieved_at = snapshot.retrieved_at.isoformat()
        candidates: list[Candidate] = []
        review: list[ReviewItem] = []
        stats: Counter[str] = Counter()

        def flag(kind: str, external_id: str | None, reason: str, **data: Any) -> None:
            review.append(
                ReviewItem(
                    source=self.name,
                    kind=kind,
                    external_id=external_id,
                    record_id=None,
                    reason=reason,
                    data=data,
                )
            )

        projects: list[Any] = doc["projects"]
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
            stage = clean_text(project["stage"])
            try:
                cw = from_aigridwatch_stage(stage, has_filing=has_filing(project))
            except UnknownStatus:
                flag("unknown_status", pid, f"unknown AI GridWatch stage {stage!r}", stage=stage)
                continue
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
                stats=stats,
            )
            if built is not None:
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
                "city": place_city or result.city,
                "municipality": place_municipality,
                "county_name": result.county_name,
                "county_fips": result.county_fips,
                "state_abbr": state,
            }
            stats[result.method or "none"] += 1

        # Parties and aliases ----------------------------------------------------------
        ref = {"source_ids": ["s1"]}

        def orgs(text: str, separators: str) -> list[dict[str, Any]]:
            out = []
            for name in split_names(text, separators):
                if looks_like_person(name) or find_personal_data(name):
                    stats["persons_dropped"] += 1
                    continue
                out.append({"name": name, **ref})
            return out

        filings = filing_names(clean_text(project.get("filing_llc")))
        parties = {
            "operator": orgs(clean_text(project.get("operator")), r"\s+/\s+"),
            "owner": orgs(clean_text(project.get("owner")), r"\s+/\s+"),
            "tenant": orgs(clean_text(project.get("tenant")), r"\s+/\s+|\s*,\s+"),
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
