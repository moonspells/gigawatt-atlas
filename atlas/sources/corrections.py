"""Cited record corrections, shared by the osm, epoch and aigridwatch importers (docs/corrections.md).

An entry of config/overrides/{source}.json has two layers. Its top-level keys change what the
source says before the importer builds (each importer's own: OSM drop, name and operator, Epoch's
location and timeline parts, AI GridWatch's releases). Its `corrections` list changes the record
built from the source: each Fix is one cited correction (the page, its verbatim quote, why and when
the reviewer decided it) with the record ops that page supports. apply_fixes applies them to the
candidate record after the build, with full provenance:

- the Fix's page becomes a source of the record (a source with the same URL is reused when it has
  no quote or the same one), which supports what the ops set, or which the items the ops add cite;
- the other sources' supports entries that covered a corrected value are narrowed to the values
  next to it (narrow_supports), and pointers into a list an op removed from are remapped
  (remap_list_pointers), so no importer source claims a value it does not state;
- a replaced value gets field_meta with method human_entered and the 07 §3.4 confidence
  (human_confidence), the old value and its sources in conflicts; removed and replaced events
  are kept in field_meta["/status_history"] conflicts;
- every op adds a record.corrections[] entry with a pointer that carries no list index;
- derived fields are recomputed, never patched: the place in canonical_name (name_suffix), the
  status and the event dates (apply_rollup), status_reason, record_type (an importer hook) and
  field_meta["/status"] when a reviewer's event decides the status.

Entries fail closed: a guard that no longer matches the record (an op's `from` or event match), a
key the input no longer has, a correction for a record that was not built, or a corrected record
that fails validate_record raises correction_error, a FetchError, so `atlas import` exits 1 and
writes nothing. place_location places a cited location by Epoch's override rules (07 §6.5).

Importing this module does no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, Self, cast

from pydantic import (
    AwareDatetime,
    Field,
    HttpUrl,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from atlas.jsonio import record_json
from atlas.schema.pointers import EXEMPT_PREFIXES, escape, is_under, resolve, split
from atlas.schema.record import (
    FUZZY_DATE_PATTERN,
    AliasKind,
    AtlasModel,
    EventType,
    FacilityRecord,
    FuzzyDate,
    Location,
    Precision,
    SourceType,
    Status,
    StatusReason,
)
from atlas.schema.rollup import STOPPED, RollupError, apply_rollup, deciding_event, period_start
from atlas.sources.base import ReviewItem, classify_source
from atlas.text import find_personal_data, hidden_characters, normalize_name, normalize_org
from atlas.validate import validate_record

if TYPE_CHECKING:
    from atlas.geo.counties import CountyIndex
    from atlas.geocode import Gazetteer
    from atlas.net import FetchError

# 07 §3.4: a value a reviewer entered from a cited source, plus the source adjustment, minus the
# conflict term when the value it replaces came from a higher-priority source type.
HUMAN_CONFIDENCE = 0.95
SOURCE_ADJUSTMENT: dict[str, float] = {
    "government_record": 0.10,
    "court_or_regulator": 0.10,
    "utility_or_iso_filing": 0.07,
    "sec_filing": 0.07,
    "company_release": 0.05,
    "trade_press": 0.0,
    "news": 0.0,
    "open_dataset": 0.0,
    "commercial_aggregator": -0.10,
    "community_report": -0.20,
}
# 07 §3.4's conflict resolution order: government record > utility or regulator filing > SEC
# filing > company release > trade press > news > open dataset > commercial aggregator >
# community report. A lower rank is a higher priority.
SOURCE_PRIORITY: dict[str, int] = {
    "government_record": 0,
    "utility_or_iso_filing": 1,
    "court_or_regulator": 1,
    "sec_filing": 2,
    "company_release": 3,
    "trade_press": 4,
    "news": 5,
    "open_dataset": 6,
    "commercial_aggregator": 7,
    "community_report": 8,
}
CONFLICT_PENALTY = 0.20
# 07 §6.4 gate 6 (§5.4): a paused, denied or cancelled status needs one of these.
STOP_SOURCE_TYPES = frozenset(
    {
        "government_record",
        "court_or_regulator",
        "utility_or_iso_filing",
        "sec_filing",
        "company_release",
        "trade_press",
        "news",
    }
)
# The paths a `set` op writes, with the JSON type of the value. City, county and coordinates change
# only through set_location (07 §6.5); ids, status, the derived dates, sources, external_ids and
# review are never set.
SET_PATHS: dict[str, str] = {
    "/location/street": "string",
    "/location/postcode": "string",
    "/capacity/it_mw": "number",
    "/capacity/facility_mw": "number",
    "/capacity/utility_request_mw": "number",
    "/capacity/backup_generation_mw": "number",
    "/capacity/mw_as_stated": "string",
    "/site/acreage": "number",
    "/site/building_sqft": "number",
    "/site/building_count": "integer",
    "/money/investment_usd": "number",
    "/money/investment_basis": "string",
    "/money/currency_year": "integer",
    "/purpose": "string",
    "/dates/expected_in_service": "date",
}
# Relative to a phase (`set` with phase): its capacity.
PHASE_SET_PATHS = {p: t for p, t in SET_PATHS.items() if p.startswith("/capacity/")}
# Precisions at which a set_location may give a street and postcode.
STREET_PRECISIONS = frozenset({"footprint", "parcel", "site", "address", "street"})
NOTE_MAX = 200
_TEXT_CONTROL = frozenset({"Cc", "Zl", "Zp"})
# A name that ends with a place suffix, " (Ashburn, VA)" or " (WY)": a rename gives the base only.
_SUFFIX_RE = re.compile(r"\((?:[^()]*, )?[A-Z]{2}\)$")
_LOCATION_FIELDS = frozenset(Location.model_fields)

Basis = Literal["out_of_date", "upstream_error", "importer_gap"]
PartyRole = Literal["operator", "owner", "developer", "tenant", "filing_entities"]
FuzzyStr = Annotated[str, Field(pattern=FUZZY_DATE_PATTERN)]


def correction_error(message: str) -> FetchError:
    """An override entry that cannot be applied: a FetchError, so `atlas import` exits 1 and
    writes nothing (atlas.net, which imports httpx, loads only here)."""
    from atlas.net import FetchError

    return FetchError(message)


def fuzzy_date(value: str) -> FuzzyDate:
    """A FuzzyDate from its value alone: YYYY-MM-DD, YYYY-MM, YYYY-Qn or YYYY."""
    precision = (
        "quarter" if "Q" in value else {10: "day", 7: "month", 4: "year"}.get(len(value), "day")
    )
    return FuzzyDate.model_validate({"value": value, "precision": precision})


def _text_problem(text: str) -> str | None:
    bad = [ch for ch in text if unicodedata.category(ch) in _TEXT_CONTROL]
    if bad or hidden_characters(text):
        return "has control or invisible characters"
    if find_personal_data(text):
        return "looks like an email address or phone number; records hold organizations only"
    return None


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _is_type(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str) and bool(value.strip())
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, str) and re.fullmatch(FUZZY_DATE_PATTERN, value) is not None


# ---------------------------------------------------------------------------- record ops


class EventMatch(AtlasModel):
    """Which event an op means, by what it says, never by its place in the list. OSM's observations
    are dated with each snapshot: match them by event and status, never by as_of."""

    event: EventType | None = None
    status: Status | None = None
    as_of: FuzzyStr | None = None
    phase: str | None = None
    planned: bool | None = None

    @model_validator(mode="after")
    def _named(self) -> Self:
        if self.event is None and self.status is None:
            raise ValueError("an event match names the event or its status")
        return self

    def hits(self, e: Mapping[str, Any]) -> bool:
        return (
            (self.event is None or e["event"] == self.event)
            and (self.status is None or e["status"] == self.status)
            and (self.as_of is None or e["as_of"]["value"] == self.as_of)
            and (self.phase is None or e["phase_id"] == self.phase)
            and (self.planned is None or e["planned"] == self.planned)
        )

    def describe(self) -> str:
        return ", ".join(f"{k}={v!r}" for k, v in self.model_dump(exclude_none=True).items())


class EventChange(AtlasModel):
    """What replace_event changes in the event it matched."""

    event: EventType | None = None
    status: Status | None = None
    as_of: FuzzyStr | None = None
    planned: bool | None = None
    note: str | None = Field(None, min_length=1, max_length=NOTE_MAX)

    @model_validator(mode="after")
    def _changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError(
                "replace_event sets at least one of event, status, as_of, planned, note"
            )
        return self


class Hold(AtlasModel):
    """Keep the record out of scope, never published (a stored one is unpublished), with a review
    item of `kind` that gives the reviewer's doubt."""

    op: Literal["hold"]
    kind: Literal["conflict", "unknown_status", "possible_duplicate", "unverified_upstream"] = (
        "conflict"
    )


class OutOfScope(AtlasModel):
    """The record is not a data center in scope (07 §2.2): a decision, kept out of scope with an
    out_of_scope item. For a whole multi-element site; OSM's element `drop` takes one element out
    of the dissolve instead."""

    op: Literal["out_of_scope"]


class SetValue(AtlasModel):
    """Set one value (SET_PATHS; with phase, a path of the phase's capacity). value null removes
    it. from is the value the record has now (a date as its value string)."""

    op: Literal["set"]
    path: str
    phase: str | None = Field(None, min_length=1)
    from_: JsonValue = Field(alias="from")
    value: JsonValue

    @model_validator(mode="after")
    def _path(self) -> Self:
        paths = SET_PATHS if self.phase is None else PHASE_SET_PATHS
        kind = paths.get(self.path)
        if kind is None:
            where = "of a phase" if self.phase is not None else "an override sets"
            raise ValueError(
                f"set: {self.path} is not a path {where} ({', '.join(sorted(paths))}); city, "
                "county and coordinates change through set_location"
            )
        for name, v in (("from", self.from_), ("value", self.value)):
            if v is not None and not _is_type(v, kind):
                raise ValueError(f"set {self.path}: {name} must be a {kind} or null, not {v!r}")
        if self.from_ == self.value:
            raise ValueError(f"set {self.path}: value equals from")
        return self


class Rename(AtlasModel):
    """Give the record another base name; the place suffix " ({place}, {ST})" stays derived. With
    old_as_alias, the old base becomes an alias of that kind."""

    op: Literal["rename"]
    from_: str = Field(alias="from", min_length=1)
    base: str = Field(min_length=1, max_length=200)
    old_as_alias: AliasKind | None = None

    @model_validator(mode="after")
    def _base(self) -> Self:
        if self.base == self.from_:
            raise ValueError("rename: base equals from")
        if _SUFFIX_RE.search(self.base):
            raise ValueError(
                f"rename: {self.base!r} ends with a place; give the base name, the place "
                "suffix is derived from the location"
            )
        return self


class AddAlias(AtlasModel):
    op: Literal["add_alias"]
    name: str = Field(min_length=1, max_length=200)
    kind: AliasKind


class RemoveAlias(AtlasModel):
    op: Literal["remove_alias"]
    name: str = Field(min_length=1)


class SetParty(AtlasModel):
    """Replace every organization of a role ([] removes them all)."""

    op: Literal["set_party"]
    role: PartyRole
    from_: list[str] = Field(alias="from")
    names: list[Annotated[str, Field(min_length=1, max_length=200)]]

    @model_validator(mode="after")
    def _names(self) -> Self:
        if len({normalize_org(n) for n in self.names}) != len(self.names):
            raise ValueError("set_party: names repeat an organization")
        if self.names == self.from_:
            raise ValueError("set_party: names equal from")
        return self


class AddParty(AtlasModel):
    op: Literal["add_party"]
    role: PartyRole
    name: str = Field(min_length=1, max_length=200)


class RemoveParty(AtlasModel):
    op: Literal["remove_party"]
    role: PartyRole
    name: str = Field(min_length=1)


class RenameParty(AtlasModel):
    """Rename one organization: its old sources still support the role, the Fix the new name."""

    op: Literal["rename_party"]
    role: PartyRole
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _differs(self) -> Self:
        if self.to == self.from_:
            raise ValueError("rename_party: to equals from")
        return self


class SetLocation(AtlasModel):
    """Move the record to a cited place, by place_location's rules (Epoch's override rules, 07
    §6.5). from: the location fields the record has now. Street and postcode only at street
    precision or better; otherwise they, geometry_ref and parcel_apns are cleared."""

    op: Literal["set_location"]
    from_: dict[str, JsonValue] = Field(alias="from", min_length=1)
    state_abbr: str = Field(pattern=r"^[A-Z]{2}$")
    county_fips: str | None = Field(None, pattern=r"^[0-9]{5}$")
    city: str | None = Field(None, min_length=1, max_length=200)
    municipality: str | None = Field(None, min_length=1, max_length=200)
    lat: float | None = Field(None, ge=18, le=72)
    lon: float | None = Field(None, ge=-180, le=-64)
    precision: Precision
    place_check: str | None = Field(None, min_length=1, max_length=300)
    street: str | None = Field(None, min_length=1, max_length=200)
    postcode: str | None = Field(None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def _fields(self) -> Self:
        unknown = sorted(set(self.from_) - _LOCATION_FIELDS)
        if unknown:
            raise ValueError(f"set_location: from names fields a location does not have: {unknown}")
        if (self.street or self.postcode) and self.precision not in STREET_PRECISIONS:
            raise ValueError(
                f"set_location: a street or postcode needs street precision or better, not "
                f"{self.precision}"
            )
        return self


class AddEvent(AtlasModel):
    """Append a cited event. status_reason only with a paused, denied or cancelled status, and
    only when the record's final status is that one."""

    op: Literal["add_event"]
    event: EventType
    status: Status
    as_of: FuzzyStr
    phase: str | None = Field(None, min_length=1)
    planned: bool = False
    note: str | None = Field(None, min_length=1, max_length=NOTE_MAX)
    status_reason: StatusReason | None = None

    @model_validator(mode="after")
    def _reason(self) -> Self:
        if self.status_reason is not None and self.status not in STOPPED:
            raise ValueError(
                f"add_event: status_reason {self.status_reason} is for paused, denied or "
                f"cancelled, not {self.status}"
            )
        return self


class ReplaceEvent(AtlasModel):
    """Change the one event match finds. A new date or status makes the Fix the event's only
    source (the original goes to conflicts); a retype keeps its sources and adds the Fix."""

    op: Literal["replace_event"]
    match: EventMatch
    set: EventChange


class RemoveEvent(AtlasModel):
    """Remove the one event match finds; it is kept in field_meta /status_history conflicts."""

    op: Literal["remove_event"]
    match: EventMatch


class Observe(AtlasModel):
    """Append an `other` event: the status the page shows on as_of, which sets the status and
    dates nothing. as_of is the date the page shows it, never defaulted."""

    op: Literal["observe"]
    status: Status
    as_of: FuzzyStr
    phase: str | None = Field(None, min_length=1)


class FirstReportOp(AtlasModel):
    """An earlier first report than every dated event: the record's first_reported events become
    `other` and a first_reported event citing the Fix is added. from: dates.first_reported now
    (null when there is none). A wrong date is corrected with replace_event instead."""

    op: Literal["first_report"]
    from_: FuzzyStr | None = Field(alias="from")
    as_of: FuzzyStr
    status: Literal["announced", "proposed", "permitted"]


class Support(AtlasModel):
    """Cite the Fix for a value the record already has (path, with from its value) or for one event
    (event), without changing it."""

    op: Literal["support"]
    path: str | None = None
    from_: JsonValue = Field(None, alias="from")
    event: EventMatch | None = None

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.path is None) == (self.event is None):
            raise ValueError("support: give path (with from) or event, not both")
        if self.path is not None:
            split(self.path)  # ValueError for a malformed pointer
            if self.from_ is None:
                raise ValueError("support: a path needs from, its value")
            if any(is_under(self.path, p) for p in EXEMPT_PREFIXES):
                raise ValueError(f"support: {self.path} needs no source")
        elif "from_" in self.model_fields_set:
            raise ValueError("support: an event match takes no from")
        return self


RecordOp = Annotated[
    Hold
    | OutOfScope
    | SetValue
    | Rename
    | AddAlias
    | RemoveAlias
    | SetParty
    | AddParty
    | RemoveParty
    | RenameParty
    | SetLocation
    | AddEvent
    | ReplaceEvent
    | RemoveEvent
    | Observe
    | FirstReportOp
    | Support,
    Field(discriminator="op"),
]
HOLD_OPS = (Hold, OutOfScope)


class Fix(AtlasModel):
    """One cited correction: the page that states it, its verbatim quote, why and when a reviewer
    decided it, and the record ops the page supports.

    quote is the span of source_url (or of archive_url, the Wayback snapshot read when the page
    refuses automated clients) that states the correction, read at retrieved_at; published_at is
    the page's own date when it states one. states_status: the status the page gives as current.
    basis says where the error came from: `out_of_date` (the source was right when written),
    `upstream_error` (the source is wrong) or `importer_gap` (the source supports the right
    value, the importer cannot read it yet: an importer fix must follow). reviewed_at is the day
    of the decision, issue_url the review PR or issue (copied to record.corrections)."""

    source_url: HttpUrl
    archive_url: HttpUrl | None = None
    publisher: str = Field(min_length=1, max_length=200)
    source_type: SourceType | None = None
    title: str | None = Field(None, min_length=1, max_length=300)
    published_at: AwareDatetime | None = None
    quote: str = Field(min_length=1, max_length=300)
    retrieved_at: AwareDatetime
    states_status: Status | None = None
    basis: Basis
    reason: str = Field(min_length=1, max_length=300)
    reviewed_at: date
    issue_url: HttpUrl | None = None
    ops: list[RecordOp] = Field(min_length=1)

    @property
    def kind(self) -> SourceType:
        """The source_type the record's source gets: the given one, else classify_source's."""
        return self.source_type or classify_source(str(self.source_url), self.title)

    @model_validator(mode="after")
    def _checks(self) -> Self:
        texts = [("publisher", self.publisher), ("quote", self.quote), ("reason", self.reason)]
        if self.title is not None:
            texts.append(("title", self.title))
        for j, op in enumerate(self.ops):
            texts += [(f"ops[{j}]", s) for s in _strings(op.model_dump(mode="json"))]
        for name, text in texts:
            problem = _text_problem(text)
            if problem:
                raise ValueError(f"{name} {problem}")
        if self.retrieved_at.astimezone(UTC).date() > self.reviewed_at:
            raise ValueError("reviewed_at is before the day of retrieved_at")
        for j, op in enumerate(self.ops):
            stopped = isinstance(op, AddEvent | Observe) and op.status in STOPPED
            if stopped and self.kind not in STOP_SOURCE_TYPES:
                raise ValueError(
                    f"ops[{j}]: a paused, denied or cancelled status needs a government, "
                    "regulator, utility or ISO, SEC, company, trade press or news source, not "
                    f"{self.kind} (07 §6.4 gate 6)"
                )
        return self


FIXES = TypeAdapter(list[Fix])


def check_fix_dates(
    key: str, fixes: Sequence[Fix], *, file: str, today: date, now: datetime
) -> None:
    """A reviewed_at, published_at or non-planned event date after today, or a retrieved_at after
    now, stops the run: the entry cites what nobody could have read yet."""
    where = f"{file} entry {key!r}"
    for i, fix in enumerate(fixes):
        published = fix.published_at.astimezone(UTC).date() if fix.published_at else None
        late = [
            name
            for name, after in (
                ("reviewed_at", fix.reviewed_at > today),
                ("published_at", published is not None and published > today),
                ("retrieved_at", fix.retrieved_at > now),
            )
            if after
        ]
        for j, op in enumerate(fix.ops):
            # replace_event's new date is checked against the event it changes, when applied.
            if isinstance(op, AddEvent | Observe | FirstReportOp):
                planned = isinstance(op, AddEvent) and op.planned
                if not planned and period_start(fuzzy_date(op.as_of)) > today:
                    late.append(f"ops[{j}] as_of {op.as_of}")
        if late:
            raise correction_error(
                f"{where}: corrections[{i}]: {', '.join(late)} is after today ({today})"
            )


# ---------------------------------------------------------------------------- confidence


def human_confidence(kind: str, replaced: Collection[str] = ()) -> float:
    """07 §3.4 for a value a reviewer entered from a source of type kind: 0.95 plus the source
    adjustment, minus 0.20 when a value it replaces came from a higher-priority source type
    (replaced: their types), clamped to 0.05-0.99. A commercial aggregator's correction of an
    open dataset's value is 0.95 - 0.10 - 0.20 = 0.65."""
    confidence = HUMAN_CONFIDENCE + SOURCE_ADJUSTMENT.get(kind, 0.0)
    rank = SOURCE_PRIORITY.get(kind)
    if rank is not None and any(SOURCE_PRIORITY.get(k, rank) < rank for k in replaced):
        confidence -= CONFLICT_PENALTY
    return round(min(0.99, max(0.05, confidence)), 2)


# ---------------------------------------------------------------------------- pointers


def _exempt(pointer: str) -> bool:
    return any(is_under(pointer, p) for p in EXEMPT_PREFIXES)


def _has_claim(doc: Any, pointer: str) -> bool:
    """True when the subtree at pointer holds a value that needs support."""
    if _exempt(pointer):
        return False
    _, node = resolve(doc, pointer)
    if node is None:
        return False
    if isinstance(node, dict):
        return any(_has_claim(doc, f"{pointer}/{escape(k)}") for k in node)
    if isinstance(node, list):
        return any(_has_claim(doc, f"{pointer}/{i}") for i in range(len(node)))
    return True


def _cites(node: Any, child: str, sid: str) -> bool:
    """False for an item of a list whose items cite their own sources (events, aliases,
    organizations) when the item does not cite sid: the source states nothing about it."""
    if not isinstance(node, list):
        return True
    item = node[int(child)]
    return not (isinstance(item, dict) and "source_ids" in item) or sid in item["source_ids"]


def _narrow_entry(doc: Any, entry: str, pointer: str, sid: str) -> list[str]:
    """A supports entry of source sid that covers pointer, rewritten into its siblings at each
    level down to pointer, keeping the ones that hold a value that needs support and, in a list
    whose items cite their own sources, only the items that cite sid (a source of one event that
    supported /status_history keeps that event, not the importer's other events)."""
    out: list[str] = []
    here = entry
    for token in split(pointer)[len(split(entry)) :]:
        _, node = resolve(doc, here)
        if isinstance(node, dict):
            children = sorted(node)
        else:
            children = [str(i) for i in range(len(node))] if isinstance(node, list) else []
        out += [
            child
            for c in children
            if c != token
            and _cites(node, c, sid)
            and _has_claim(doc, child := f"{here}/{escape(c)}")
        ]
        here = f"{here}/{escape(token)}"
    return out


def narrow_supports(doc: dict[str, Any], pointer: str, *, keep: str | None = None) -> list[str]:
    """Take pointer out of every source's supports but keep's, in place: an entry that covers it
    becomes its siblings (narrowing: Epoch's "/parties" becomes "/parties/tenant/1" when tenant 0
    is renamed), an entry inside it is dropped. Returns the ids of the sources that supported it."""
    losers: list[str] = []
    for source in doc["sources"]:
        if source["id"] == keep:
            continue
        new: list[str] = []
        hit = False
        for entry in source["supports"]:
            if entry and is_under(pointer, entry):
                hit = True
                new += _narrow_entry(doc, entry, pointer, source["id"])
            elif entry and is_under(entry, pointer):
                hit = True
            else:
                new.append(entry)
        if hit:
            losers.append(source["id"])
            source["supports"] = list(dict.fromkeys(new))
    return losers


def remap_list_pointers(doc: dict[str, Any], list_pointer: str, index: int) -> None:
    """After list_pointer/index was removed, in place: supports entries and field_meta keys into
    that item are dropped and those into later items shifted down by one."""
    depth = len(split(list_pointer))

    def remap(pointer: str) -> str | None:
        if pointer == list_pointer or not is_under(pointer, list_pointer):
            return pointer
        tokens = split(pointer)
        k = int(tokens[depth])
        if k == index:
            return None
        if k > index:
            tokens[depth] = str(k - 1)
        return "".join(f"/{escape(t)}" for t in tokens)

    for source in doc["sources"]:
        source["supports"] = [p for e in source["supports"] if (p := remap(e)) is not None]
    doc["field_meta"] = {p: m for k, m in doc["field_meta"].items() if (p := remap(k)) is not None}


# ---------------------------------------------------------------------------- places


def place_label(location: Mapping[str, Any], gazetteer: Gazetteer) -> str | None:
    """The place a record's name gives: the city or municipality, else the county's full name."""
    where = location.get("city") or location.get("municipality")
    if not where and location.get("county_fips"):
        where = gazetteer.county_full_name(location["county_fips"]) or location.get("county_name")
    return where or None


def name_suffix(location: Mapping[str, Any], gazetteer: Gazetteer) -> str:
    """The end of every canonical_name: " ({place}, {ST})", or " ({ST})" without a place."""
    where = place_label(location, gazetteer)
    state = location["state_abbr"]
    return f" ({where}, {state})" if where else f" ({state})"


class PlaceSpec(Protocol):
    """A cited place: Epoch's LocationOverride, or a set_location op."""

    @property
    def state_abbr(self) -> str: ...
    @property
    def county_fips(self) -> str | None: ...
    @property
    def city(self) -> str | None: ...
    @property
    def municipality(self) -> str | None: ...
    @property
    def lat(self) -> float | None: ...
    @property
    def lon(self) -> float | None: ...
    @property
    def precision(self) -> Precision: ...
    @property
    def place_check(self) -> str | None: ...


def place_location(
    spec: PlaceSpec,
    *,
    where: str,
    counties: CountyIndex,
    gazetteer: Gazetteer,
    postal_city: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """The location a cited place describes and the place its name gives (the city or
    municipality, else the county's full name), checked like `atlas validate` rule 5 would; a
    point never lies outside the county the entry names, at any precision (as in
    atlas.geocode). A problem raises correction_error(f"{where}: ...").

    A county entry names no city or municipality, since the county's point need not lie in it
    (AWS Berwick's Luzerne County point is in Rice township, its campus in Salem Township). A
    municipality (a township, a New England town) is placed at its county subdivision's Gazetteer
    point in the entry's county, at `locality`. A city that is also postal_city (the city of
    Epoch's address) counts only with place_check: the postal city is only near the site."""
    from atlas.geocode import (
        COUNTY_CHECKED_PRECISIONS,
        POINT_IN_POLYGON_PRECISIONS,
        normalize_place,
    )

    def bad(why: str) -> FetchError:
        return correction_error(f"{where}: {why}")

    county = counties.get(spec.county_fips) if spec.county_fips else None
    if spec.county_fips and (county is None or county.state_abbr != spec.state_abbr):
        raise bad(f"county_fips {spec.county_fips} is not a county of {spec.state_abbr}")
    if spec.precision == "county" and (spec.city or spec.municipality):
        raise bad(
            "a county entry names no city or municipality: the county's point need not lie in "
            "it (give precision locality to place the site at the municipality)"
        )
    if (
        spec.precision == "locality"
        and spec.city
        and postal_city
        and normalize_place(spec.city) == normalize_place(postal_city)
        and spec.place_check is None
    ):
        raise bad(
            f"{spec.city!r} is also the postal city of Epoch's address, which is only near the "
            "site (07 §6.5): say in place_check how the site was found inside the city's Census "
            "place polygon, or place it at the county a source states"
        )
    lat, lon = spec.lat, spec.lon
    if (lat is None) != (lon is None):
        raise bad("set both lat and lon, or neither")
    method: str | None = "manual"
    what = f"({lat}, {lon})"
    if lat is None or lon is None:
        if spec.precision == "county" and county is not None:
            lat, lon = counties.centroid(county.fips)
            method = "county_centroid"
        elif spec.precision == "locality" and spec.city:
            hit = gazetteer.place(spec.state_abbr, spec.city)
            if hit is None:
                raise bad(f"no Gazetteer place {spec.city!r} in {spec.state_abbr}")
            lat, lon = hit[0], hit[1]
            method = "gazetteer"
            what = f"the Gazetteer place {spec.city!r}"
        elif spec.precision == "locality" and spec.municipality:
            if county is None:
                raise bad(
                    "a municipality needs county_fips: the same name is used in many counties"
                )
            sub = gazetteer.cousub_entry(spec.state_abbr, spec.municipality, county.fips)
            if sub is None:
                raise bad(
                    f"no Gazetteer county subdivision {spec.municipality!r} in county {county.fips}"
                )
            lat, lon = sub.lat, sub.lon
            method = "gazetteer"
            what = f"the Gazetteer county subdivision {spec.municipality!r}"
        elif spec.precision in ("state", "unknown"):
            method = None
        elif spec.precision == "county":
            raise bad("precision county needs county_fips (or lat and lon)")
        elif spec.precision == "locality":
            raise bad("precision locality needs city or municipality (or lat and lon)")
        else:
            raise bad(f"precision {spec.precision} needs lat and lon")
    elif spec.precision in POINT_IN_POLYGON_PRECISIONS and county is None:
        county = counties.lookup(lat, lon)
    if lat is not None and lon is not None:
        if spec.precision in COUNTY_CHECKED_PRECISIONS:
            if county is None or not counties.contains(county.fips, lat, lon):
                raise bad(f"({lat}, {lon}) is not inside the county")
        elif not counties.in_state(spec.state_abbr, lat, lon):
            raise bad(f"({lat}, {lon}) is not inside {spec.state_abbr}")
        elif county is not None and not counties.contains(county.fips, lat, lon):
            # A record never names a county its point lies outside of (the geocode chain's rule).
            raise bad(f"{what} is not inside county {county.fips}")
    location = {
        "lat": lat,
        "lon": lon,
        "precision": spec.precision,
        "city": spec.city,
        "municipality": spec.municipality,
        "county_name": county.name if county else None,
        "county_fips": county.fips if county else None,
        "state_abbr": spec.state_abbr,
        "geocode_method": method,
    }
    label = (
        spec.city
        or spec.municipality
        or (gazetteer.county_full_name(county.fips) if county else None)
    )
    return location, label


# ---------------------------------------------------------------------------- apply


@dataclass(frozen=True)
class Applied:
    """The corrected record and the review items its hold and out_of_scope ops file."""

    record: FacilityRecord
    review: list[ReviewItem]


def _projection(value: Any) -> Any:
    """What a guard compares: an organization list as its names, a fuzzy date as its value."""
    if (
        isinstance(value, list)
        and value
        and all(isinstance(v, dict) and "name" in v for v in value)
    ):
        return [v["name"] for v in value]
    if isinstance(value, dict) and set(value) == {"value", "precision"}:
        return value["value"]
    return value


def _summary(e: Mapping[str, Any]) -> dict[str, JsonValue]:
    """An event as a corrections[] entry or a conflict gives it."""
    return {
        "event": e["event"],
        "status": e["status"],
        "as_of": e["as_of"]["value"],
        "phase_id": e["phase_id"],
        "planned": e["planned"],
    }


def _sid_key(sid: str) -> int:
    return int(sid[1:])


def _cite(doc: dict[str, Any], fix: Fix) -> str:
    """The id of the record's source for fix's page: a source with the same URL that has no quote
    or the same quote takes the Fix's citation; otherwise a new source is added."""
    url = str(HttpUrl(str(fix.source_url)))
    for source in doc["sources"]:
        if str(HttpUrl(str(source["url"]))) != url or source["quote"] not in (None, fix.quote):
            continue
        source.update(
            quote=fix.quote,
            quote_match="human",
            retrieved_at=fix.retrieved_at.isoformat(),
            publisher=fix.publisher,
            source_type=fix.source_type or source["source_type"],
        )
        if fix.archive_url is not None:
            source["archive_url"] = str(fix.archive_url)
        if fix.published_at is not None and source["published_at"] is None:
            source["published_at"] = fix.published_at.isoformat()
        return str(source["id"])
    sid = f"s{max(_sid_key(s['id']) for s in doc['sources']) + 1}"
    doc["sources"].append(
        {
            "id": sid,
            "url": str(fix.source_url),
            "archive_url": str(fix.archive_url) if fix.archive_url else None,
            "publisher": fix.publisher,
            "title": fix.title,
            "source_type": fix.kind,
            "license": None,
            "published_at": fix.published_at.isoformat() if fix.published_at else None,
            "retrieved_at": fix.retrieved_at.isoformat(),
            "text_sha256": None,
            "quote": fix.quote,
            "quote_start": None,
            "quote_end": None,
            "quote_match": "human",
            "supports": [],
        }
    )
    return sid


class _Patch:
    """apply_fixes' state: the record as JSON, its base name and what the ops did so far."""

    def __init__(
        self,
        record: FacilityRecord,
        *,
        file: str,
        source: str,
        external_id: str | None,
        today: date,
        counties: CountyIndex,
        gazetteer: Gazetteer,
        postal_city: str | None,
        where: str,
    ) -> None:
        self.doc = cast("dict[str, Any]", record_json(record))
        self.file = file
        self.source = source
        self.external_id = external_id
        self.today = today
        self.counties = counties
        self.gazetteer = gazetteer
        self.postal_city = postal_city
        self.review: list[ReviewItem] = []
        self.event_sids: set[str] = set()
        self.stop_reason: tuple[Status, StatusReason, str] | None = None
        self.key = ""
        self.where = where
        suffix = name_suffix(self.doc["location"], gazetteer)
        name: str = self.doc["canonical_name"]
        if not name.endswith(suffix) or len(name) == len(suffix):
            raise self.fail(
                f"the record {name!r} does not end with its place {suffix!r}, so its base name "
                "is unknown"
            )
        self.base = name[: -len(suffix)]

    # -- helpers

    def fail(self, why: str) -> FetchError:
        return correction_error(f"{self.where}: {why}")

    def stale(self, why: str) -> FetchError:
        return self.fail(f"{why} (stale: review the entry)")

    @property
    def events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = self.doc["status_history"]
        return events

    def source_of(self, sid: str) -> dict[str, Any]:
        found: dict[str, Any] = next(s for s in self.doc["sources"] if s["id"] == sid)
        return found

    def kinds(self, sids: Collection[str]) -> list[str]:
        return [s["source_type"] for s in self.doc["sources"] if s["id"] in sids]

    def supports(self, sid: str, pointer: str) -> None:
        entries: list[str] = self.source_of(sid)["supports"]
        if pointer not in entries:
            entries.append(pointer)

    def take_over(self, pointer: str, sid: str) -> list[str]:
        """Narrow the other sources' support of pointer and drop field_meta inside it; the ids of
        the sources that supported the old value (supports and field_meta)."""
        losers = set(narrow_supports(self.doc, pointer, keep=sid))
        meta: dict[str, Any] = self.doc["field_meta"]
        for k in list(meta):
            if is_under(pointer, k) or is_under(k, pointer):
                losers.update(meta[k]["source_ids"])
            if is_under(k, pointer):
                del meta[k]
        losers.discard(sid)
        return sorted(losers, key=_sid_key)

    def meta(self, pointer: str, fix: Fix, sid: str, losers: list[str], old: Any) -> None:
        conflicts: list[dict[str, Any]] = []
        if old is not None:
            conflicts.append(
                {"value": old, "source_ids": losers, "note": "replaced by a cited correction"}
            )
        self.doc["field_meta"][pointer] = {
            "confidence": human_confidence(fix.kind, self.kinds(losers)),
            "method": "human_entered",
            "source_ids": [sid],
            "conflicts": conflicts,
        }

    def note(self, fix: Fix, pointer: str, old: Any, new: Any) -> None:
        self.doc["corrections"].append(
            {
                "on": fix.reviewed_at.isoformat(),
                "pointer": pointer,
                "old": old,
                "new": new,
                "reason": fix.reason,
                "issue_url": str(fix.issue_url) if fix.issue_url else None,
            }
        )

    def one_event(self, match: EventMatch) -> int:
        hits = [k for k, e in enumerate(self.events) if match.hits(e)]
        if len(hits) != 1:
            raise self.stale(f"{len(hits)} events match {match.describe()}, not 1")
        return hits[0]

    def phase_index(self, phase: str) -> int:
        for k, p in enumerate(self.doc["phases"]):
            if p["phase_id"] == phase:
                return k
        raise self.fail(f"the record has no phase {phase!r}")

    def lost_event(self, fix: Fix, sid: str, e: Mapping[str, Any]) -> None:
        """Keep a removed or redated event in field_meta /status_history conflicts."""
        meta = self.doc["field_meta"].setdefault(
            "/status_history",
            {
                "confidence": human_confidence(fix.kind),
                "method": "human_entered",
                "source_ids": [],
                "conflicts": [],
            },
        )
        if sid not in meta["source_ids"]:
            meta["source_ids"] = sorted([*meta["source_ids"], sid], key=_sid_key)
        meta["conflicts"].append(
            {"value": _summary(e), "source_ids": list(e["source_ids"]), "note": fix.reason[:200]}
        )

    def append_event(self, sid: str, event: dict[str, Any]) -> dict[str, Any]:
        event["seq"] = max((e["seq"] for e in self.events), default=0) + 1
        event["source_ids"] = [sid]
        self.events.append(event)
        narrow_supports(self.doc, f"/status_history/{len(self.events) - 1}", keep=sid)
        self.event_sids.add(sid)
        return event

    def item(self, fix: Fix, kind: str, reason: str) -> None:
        self.review.append(
            ReviewItem(
                source=self.source,
                kind=kind,
                external_id=self.external_id if self.external_id is not None else self.key,
                record_id=None,
                reason=reason,
                data={
                    "source_url": str(fix.source_url),
                    "quote": fix.quote,
                    "reviewed_at": fix.reviewed_at.isoformat(),
                    "basis": fix.basis,
                    "file": self.file,
                },
            )
        )

    # -- the ops

    def apply(self, fix: Fix, sid: str, op: RecordOp) -> None:
        doc = self.doc
        if isinstance(op, HOLD_OPS):
            if doc["scope"] == "in_scope":
                doc["scope"] = "out_of_scope"
                self.note(fix, "/scope", "in_scope", "out_of_scope")
            entry = f"{self.file} entry {self.key!r} (reviewed {fix.reviewed_at})"
            if isinstance(op, OutOfScope):
                self.item(
                    fix,
                    "out_of_scope",
                    f"out of scope by {entry}: {fix.reason} ({fix.source_url}); kept with scope "
                    "out_of_scope (07 §2.2)",
                )
            else:
                self.item(
                    fix,
                    op.kind,
                    f"held by {entry}: {fix.reason} ({fix.source_url}); kept with scope "
                    "out_of_scope until a reviewer resolves it",
                )
        elif isinstance(op, SetValue):
            self.set_value(fix, sid, op)
        elif isinstance(op, Rename):
            self.rename(fix, sid, op)
        elif isinstance(op, AddAlias):
            aliases: list[dict[str, Any]] = doc["aliases"]
            name = normalize_name(op.name)
            if name == normalize_name(self.base):
                raise self.fail(f"alias {op.name!r} is the record's name")
            if any(normalize_name(a["name"]) == name for a in aliases):
                raise self.stale(f"the record has the alias {op.name!r}")
            aliases.append({"name": op.name, "kind": op.kind, "source_ids": [sid]})
            narrow_supports(doc, f"/aliases/{len(aliases) - 1}", keep=sid)
            self.note(fix, "/aliases", None, op.name)
        elif isinstance(op, RemoveAlias):
            k = next((k for k, a in enumerate(doc["aliases"]) if a["name"] == op.name), None)
            if k is None:
                raise self.stale(f"the record has no alias {op.name!r}")
            doc["aliases"].pop(k)
            remap_list_pointers(doc, "/aliases", k)
            self.note(fix, "/aliases", op.name, None)
        elif isinstance(op, SetParty | AddParty | RemoveParty | RenameParty):
            self.party(fix, sid, op)
        elif isinstance(op, SetLocation):
            self.set_location(fix, sid, op)
        elif isinstance(op, AddEvent | Observe | FirstReportOp):
            self.add_event(fix, sid, op)
        elif isinstance(op, ReplaceEvent):
            self.replace_event(fix, sid, op)
        elif isinstance(op, RemoveEvent):
            k = self.one_event(op.match)
            removed = self.events.pop(k)
            remap_list_pointers(doc, "/status_history", k)
            self.lost_event(fix, sid, removed)
            self.note(fix, "/status_history", _summary(removed), None)
        elif isinstance(op, Support):
            self.support(fix, sid, op)

    def set_value(self, fix: Fix, sid: str, op: SetValue) -> None:
        if op.phase is None:
            pointer = semantic = op.path
        else:
            pointer = f"/phases/{self.phase_index(op.phase)}{op.path}"
            semantic = f"/phases/{escape(op.phase)}{op.path}"
        found, current = resolve(self.doc, pointer)
        old = _projection(current) if found else None
        if old != op.from_:
            raise self.stale(f"{pointer} is {old!r}, not {op.from_!r}")
        losers = self.take_over(pointer, sid)
        parent_pointer, leaf = pointer.rsplit("/", 1)
        _, parent = resolve(self.doc, parent_pointer)
        if not isinstance(parent, dict):
            raise self.fail(f"{parent_pointer} is not an object")
        if op.path.startswith("/dates/"):
            if op.value is None:
                parent.pop(leaf, None)
            else:
                parent[leaf] = fuzzy_date(str(op.value)).model_dump(mode="json")
        else:
            parent[leaf] = op.value
        if op.value is not None:
            self.supports(sid, pointer)
        self.meta(pointer, fix, sid, losers, old)
        self.note(fix, semantic, old, op.value)

    def rename(self, fix: Fix, sid: str, op: Rename) -> None:
        if self.base != op.from_:
            raise self.stale(f"the name is {self.base!r}, not {op.from_!r}")
        suffix = name_suffix(self.doc["location"], self.gazetteer)
        old_name = self.base + suffix
        losers = self.take_over("/canonical_name", sid)
        aliases: list[dict[str, Any]] = self.doc["aliases"]
        for k in reversed(range(len(aliases))):
            if normalize_name(aliases[k]["name"]) == normalize_name(op.base):
                aliases.pop(k)
                remap_list_pointers(self.doc, "/aliases", k)
        if op.old_as_alias is not None and not any(
            normalize_name(a["name"]) == normalize_name(self.base) for a in aliases
        ):
            aliases.append(
                {"name": self.base, "kind": op.old_as_alias, "source_ids": losers or [sid]}
            )
        self.base = op.base
        self.supports(sid, "/canonical_name")
        self.meta("/canonical_name", fix, sid, losers, old_name)
        self.note(fix, "/canonical_name", old_name, op.base + suffix)

    def party(
        self, fix: Fix, sid: str, op: SetParty | AddParty | RemoveParty | RenameParty
    ) -> None:
        pointer = f"/parties/{op.role}"
        refs: list[dict[str, Any]] = self.doc["parties"][op.role]
        names = [r["name"] for r in refs]
        if isinstance(op, SetParty):
            if names != op.from_:
                raise self.stale(f"{op.role} is {names}, not {op.from_}")
            losers = set(self.take_over(pointer, sid))
            losers.update(s for r in refs for s in r["source_ids"])
            losers.discard(sid)
            org_ids = {r["name"]: r["org_id"] for r in refs}
            self.doc["parties"][op.role] = [
                {"name": n, "org_id": org_ids.get(n), "source_ids": [sid]} for n in op.names
            ]
            self.meta(pointer, fix, sid, sorted(losers, key=_sid_key), names or None)
            self.note(fix, pointer, names, op.names)
        elif isinstance(op, AddParty):
            if any(normalize_org(n) == normalize_org(op.name) for n in names):
                raise self.stale(f"{op.role} {op.name!r} is in the record")
            refs.append({"name": op.name, "org_id": None, "source_ids": [sid]})
            narrow_supports(self.doc, f"{pointer}/{len(refs) - 1}", keep=sid)
            self.note(fix, pointer, None, op.name)
        else:
            target = op.name if isinstance(op, RemoveParty) else op.from_
            k = next((k for k, n in enumerate(names) if n == target), None)
            if k is None:
                raise self.stale(f"the record has no {op.role} {target!r}")
            if isinstance(op, RemoveParty):
                refs.pop(k)
                remap_list_pointers(self.doc, pointer, k)
                self.note(fix, pointer, op.name, None)
                return
            if any(normalize_org(n) == normalize_org(op.to) for i, n in enumerate(names) if i != k):
                raise self.fail(f"{op.role} {op.to!r} is in the record already")
            refs[k]["name"] = op.to
            refs[k]["source_ids"] = sorted({*refs[k]["source_ids"], sid}, key=_sid_key)
            narrow_supports(self.doc, f"{pointer}/{k}", keep=sid)
            self.note(fix, pointer, op.from_, op.to)

    def set_location(self, fix: Fix, sid: str, op: SetLocation) -> None:
        location: dict[str, Any] = self.doc["location"]
        for name, value in op.from_.items():
            if location.get(name) != value:
                raise self.stale(f"location.{name} is {location.get(name)!r}, not {value!r}")
        placed, _ = place_location(
            op,
            where=self.where,
            counties=self.counties,
            gazetteer=self.gazetteer,
            postal_city=self.postal_city,
        )
        new = {
            **placed,
            "street": op.street,
            "postcode": op.postcode,
            "geometry_ref": None,
            "parcel_apns": [],
        }
        old = {k: v for k, v in location.items() if new.get(k, v) != v}
        if not old:
            raise self.stale("the record is at the place set_location gives already")
        changed = {k: new[k] for k in old}
        losers = self.take_over("/location", sid)
        location.update(new)
        self.supports(sid, "/location")
        self.meta("/location", fix, sid, losers, old or None)
        self.note(fix, "/location", old, changed)

    def add_event(self, fix: Fix, sid: str, op: AddEvent | Observe | FirstReportOp) -> None:
        as_of = fuzzy_date(op.as_of)
        phase = None if isinstance(op, FirstReportOp) else op.phase
        event: dict[str, Any]
        retyped: list[JsonValue] = []
        if phase is not None:
            self.phase_index(phase)
        if isinstance(op, AddEvent):
            if any(
                e["event"] == op.event
                and e["as_of"]["value"] == op.as_of
                and e["phase_id"] == phase
                for e in self.events
            ):
                raise self.stale(f"the record has {op.event} on {op.as_of} already")
            event = {
                "event": op.event,
                "status": op.status,
                "planned": op.planned,
                "note": op.note,
            }
            if op.status_reason is not None:
                self.stop_reason = (op.status, op.status_reason, self.where)
        elif isinstance(op, Observe):
            if any(
                e["event"] == "other"
                and e["status"] == op.status
                and e["as_of"]["value"] == op.as_of
                and e["phase_id"] == phase
                for e in self.events
            ):
                raise self.stale(f"the record observes {op.status} on {op.as_of} already")
            note = f"Status per {fix.publisher} on {op.as_of}, not the date it began"
            event = {
                "event": "other",
                "status": op.status,
                "planned": False,
                "note": note[:NOTE_MAX],
            }
        else:
            first = self.doc["dates"].get("first_reported")
            current = first["value"] if first else None
            if current != op.from_:
                raise self.stale(f"dates.first_reported is {current!r}, not {op.from_!r}")
            start = period_start(as_of)
            later = [
                e
                for e in self.events
                if not e["planned"]
                and e["event"] != "other"
                and period_start(FuzzyDate.model_validate(e["as_of"])) <= start
            ]
            if later:
                raise self.fail(
                    f"first report {op.as_of} is not earlier than the record's "
                    f"{later[0]['event']} on {later[0]['as_of']['value']} (correct a wrong date "
                    "with replace_event)"
                )
            for e in self.events:
                if e["event"] == "first_reported" and not e["planned"]:
                    retyped.append(_summary(e))
                    e["event"] = "other"
            note = f"First report, per {fix.publisher}"
            event = {
                "event": "first_reported",
                "status": op.status,
                "planned": False,
                "note": note[:NOTE_MAX],
            }
        planned = bool(event["planned"])
        if not planned and period_start(as_of) > self.today:
            raise self.fail(f"{op.as_of} is after today ({self.today}) but not planned")
        event.update(as_of=as_of.model_dump(mode="json"), phase_id=phase)
        added = self.append_event(sid, event)
        old: JsonValue = (retyped or None) if isinstance(op, FirstReportOp) else None
        self.note(fix, "/status_history", old, _summary(added))

    def replace_event(self, fix: Fix, sid: str, op: ReplaceEvent) -> None:
        k = self.one_event(op.match)
        event = self.events[k]
        before = dict(event)
        change = op.set
        if change.event is not None:
            event["event"] = change.event
        if change.status is not None:
            event["status"] = change.status
        if change.as_of is not None:
            event["as_of"] = fuzzy_date(change.as_of).model_dump(mode="json")
        if change.planned is not None:
            event["planned"] = change.planned
        if "note" in change.model_fields_set:
            event["note"] = change.note
        if event == before:
            raise self.stale(f"the event {_summary(before)} has the values replace_event sets")
        if not event["planned"] and period_start(FuzzyDate.model_validate(event["as_of"])) > (
            self.today
        ):
            raise self.fail(
                f"{event['as_of']['value']} is after today ({self.today}) but not planned"
            )
        if event["as_of"] != before["as_of"] or event["status"] != before["status"]:
            self.lost_event(fix, sid, before)
            event["source_ids"] = [sid]
        else:
            event["source_ids"] = sorted({*event["source_ids"], sid}, key=_sid_key)
        narrow_supports(self.doc, f"/status_history/{k}", keep=sid)
        self.event_sids.add(sid)
        self.note(fix, "/status_history", _summary(before), _summary(event))

    def support(self, fix: Fix, sid: str, op: Support) -> None:
        if op.event is not None:
            k = self.one_event(op.event)
            event = self.events[k]
            if sid in event["source_ids"]:
                raise self.stale(f"the event {_summary(event)} cites {fix.source_url} already")
            event["source_ids"] = sorted([*event["source_ids"], sid], key=_sid_key)
            self.event_sids.add(sid)
            self.note(fix, "/status_history", _summary(event), _summary(event))
            return
        if op.path is None:
            raise self.fail("support names no path and no event")
        found, current = resolve(self.doc, op.path)
        value = _projection(current) if found else None
        if not found or value != op.from_:
            raise self.stale(f"{op.path} is {value!r}, not {op.from_!r}")
        if any(e and is_under(op.path, e) for e in self.source_of(sid)["supports"]):
            raise self.stale(f"{op.path} cites {fix.source_url} already")
        self.supports(sid, op.path)
        self.note(fix, op.path, value, value)

    # -- derived fields

    def finish(
        self, record_type: Callable[[FacilityRecord], Literal["campus", "project"]] | None
    ) -> FacilityRecord:
        doc = self.doc
        doc["canonical_name"] = self.base + name_suffix(doc["location"], self.gazetteer)
        if not any(not e["planned"] for e in doc["status_history"]):
            raise self.fail("the corrections leave no event that has happened")
        try:
            record = apply_rollup(FacilityRecord.model_validate(doc))
        except (ValidationError, RollupError) as e:
            raise self.fail(f"the corrected record is not a valid record: {e}") from e
        update: dict[str, Any] = {}
        if self.stop_reason is not None:
            status, reason, where = self.stop_reason
            if record.status != status:
                raise correction_error(
                    f"{where}: status_reason {reason} is for {status}, but the record's status "
                    f"is {record.status}"
                )
            update["status_reason"] = reason
        elif record.status not in STOPPED:
            update["status_reason"] = None
        if record_type is not None:
            update["record_type"] = record_type(record)
        decided = deciding_event(record)
        cited = [s for s in decided.source_ids if s in self.event_sids]
        out = record_json(record) | update
        if cited:
            kinds = {s.id: s.source_type for s in record.sources}
            out["field_meta"]["/status"] = {
                "confidence": max(human_confidence(kinds[s]) for s in cited),
                "method": "human_entered",
                "source_ids": list(decided.source_ids),
                "conflicts": [],
            }
        try:
            record = FacilityRecord.model_validate(out)
        except ValidationError as e:
            raise self.fail(f"the corrected record is not a valid record: {e}") from e
        issues = validate_record(record, counties=self.counties, today=self.today)
        if issues:
            raise self.fail(
                "the corrected record fails validation: "
                + "; ".join(f"{i.pointer} [{i.rule}] {i.message}" for i in issues)
            )
        return record


def apply_fixes(
    record: FacilityRecord,
    entries: Sequence[tuple[str, Sequence[Fix]]],
    *,
    file: str,
    source: str,
    today: date,
    counties: CountyIndex,
    gazetteer: Gazetteer,
    external_id: str | None = None,
    record_type: Callable[[FacilityRecord], Literal["campus", "project"]] | None = None,
    postal_city: str | None = None,
) -> Applied:
    """The record with the corrections of entries applied: (key, fixes) pairs in the source's key
    order (OSM ref_key, Epoch name, AI GridWatch id), each entry's Fixes in file order and each
    Fix's ops in order, so a second op on a value it changed must name the new value in `from`.

    file names the overrides file in messages; source and external_id (default: the entry's
    key) go on the review items that hold and out_of_scope ops file. record_type, when given,
    recomputes record_type from the corrected record (Epoch: campus once a building operates);
    postal_city is the city of the source's postal address, for set_location's place check.
    A guard that does not match, or a corrected record that fails validate_record, raises
    correction_error naming the entry and the op."""
    keys = ", ".join(repr(key) for key, _ in entries)
    where = f"{file} {'entries' if len(entries) > 1 else 'entry'} {keys}"
    patch = _Patch(
        record,
        where=where,
        file=file,
        source=source,
        external_id=external_id,
        today=today,
        counties=counties,
        gazetteer=gazetteer,
        postal_city=postal_city,
    )
    for key, fixes in entries:
        patch.key = key
        for i, fix in enumerate(fixes):
            sid = _cite(patch.doc, fix)
            for j, op in enumerate(fix.ops):
                patch.where = f"{file} entry {key!r}: corrections[{i}].ops[{j}] ({op.op})"
                patch.apply(fix, sid, op)
    patch.where = where
    return Applied(patch.finish(record_type), patch.review)


# ---------------------------------------------------------------------------- entries


def _names_key(item: ReviewItem, key: str) -> bool:
    if item.external_id == key:
        return True
    return any(v == key or (isinstance(v, list) and key in v) for v in item.data.values())


def has_first_report(fixes: Iterable[Fix]) -> bool:
    """True when a Fix has a first_report op: the cited first report the importer's first-report
    hold asks for (Epoch AI's and AI GridWatch's lift it)."""
    return any(isinstance(op, FirstReportOp) for fix in fixes for op in fix.ops)


def check_expect(
    key: str,
    expect: Mapping[str, JsonValue],
    found: Mapping[str, JsonValue],
    *,
    file: str,
    what: str,
) -> None:
    """Stop the run when an entry's expect guard ({field: value, null for an empty one}) does not
    match what the input has for its key (found: the input's fields, what: "row" or "site's CSV
    row" in messages): the source changed upstream since the reviewer read it. A field the input
    does not have stops it too, so a misspelt guard never passes."""
    where = f"{file} entry {key!r}"
    missing = sorted(k for k in expect if k not in found)
    if missing:
        raise correction_error(
            f"{where}: expect names {missing}, which the {what} does not have; review the entry"
        )
    wrong = {k: found[k] for k, v in expect.items() if found[k] != v}
    if wrong:
        expected = {k: expect[k] for k in wrong}
        raise correction_error(
            f"{where}: expect {expected}, but the {what} has {wrong}; review the entry (the "
            f"{what} changed upstream)"
        )


def check_entries(
    entries: Mapping[str, Sequence[Fix]],
    *,
    present: Collection[str],
    built: Collection[str],
    review: Sequence[ReviewItem],
    file: str,
) -> None:
    """Stop the run (correction_error) when an entry no longer applies:

    - its key (every entry of the file, with or without corrections) is not in present, the
      input's keys: the row, site or element is gone or renamed upstream;
    - it has record ops other than hold and out_of_scope, and no built candidate holds its key
      (built: the match values of every candidate): the importer held it, and the message gives
      the review items that name the key.

    An entry whose ops only hold or put out of scope a key the importer already held is fine."""
    unknown = sorted(k for k in entries if k not in present)
    if unknown:
        raise correction_error(
            f"{file} names {', '.join(repr(k) for k in unknown)}, which this input does not "
            "have; review the entries"
        )
    for key in sorted(entries):
        ops = [op for fix in entries[key] for op in fix.ops]
        if key in built or all(isinstance(op, HOLD_OPS) for op in ops):
            continue
        held = [f"{i.kind}: {i.reason}" for i in review if _names_key(i, key)]
        raise correction_error(
            f"{file} entry {key!r}: no record was built for {key} (held: "
            f"{'; '.join(held) or 'no review item names it'}); release the check or remove the "
            "correction"
        )


def mark_corrected(
    review: Sequence[ReviewItem], entries: Mapping[str, Sequence[Fix]], *, file: str
) -> list[ReviewItem]:
    """The review items, those about a key whose corrections were applied annotated with
    data.corrected_by ("{file} (reviewed {date}): {reasons}"), so the review page shows the
    decision. Items are annotated, never dropped."""
    out: list[ReviewItem] = []
    for item in review:
        fixes = entries.get(item.external_id or "") or []
        if fixes:
            day = max(f.reviewed_at for f in fixes)
            reasons = "; ".join(dict.fromkeys(f.reason for f in fixes))
            data = {**item.data, "corrected_by": f"{file} (reviewed {day}): {reasons}"}
            item = item.model_copy(update={"data": data})
        out.append(item)
    return out


def correction_metrics(entries: Mapping[str, Sequence[Fix]], *, corrected: int) -> dict[str, int]:
    """The receipt's counts of the overrides file's corrections: entries with corrections, ops,
    records corrected (corrected, from the importer), hold and out_of_scope ops, and Fixes whose
    basis is importer_gap (each needs an importer fix; the count should fall to 0)."""
    fixes = [fix for fs in entries.values() for fix in fs]
    ops = [op for fix in fixes for op in fix.ops]
    return {
        "corrections_entries": sum(1 for fs in entries.values() if fs),
        "corrections_ops": len(ops),
        "records_corrected": corrected,
        "corrections_held": sum(1 for op in ops if isinstance(op, Hold)),
        "corrections_out_of_scope": sum(1 for op in ops if isinstance(op, OutOfScope)),
        "corrections_importer_gap": sum(1 for fix in fixes if fix.basis == "importer_gap"),
    }
