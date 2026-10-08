"""`atlas validate`: the record rules of 07 §3.6.

validate_record runs the rules that need only the record (and the county polygons);
apply_import runs it on every candidate. validate_dataset adds the file, JSON Schema, merged_into
and org rules over a records directory.

Rule ids: file, schema, jsonschema, rollup, dates, sources, support, quote, merged_into, geo,
range, privacy, text, org, phase, scope.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from atlas.geo.fips import IN_SCOPE, state_by_abbr
from atlas.jsonio import dumps_pretty, loads, record_json
from atlas.schema.export import render
from atlas.schema.org import Org
from atlas.schema.pointers import escape, resolve, unsupported_pointers
from atlas.schema.record import FacilityRecord, FuzzyDate
from atlas.schema.rollup import (
    DERIVED_DATE_KEYS,
    RollupError,
    derive_dates,
    period_start,
    rollup_status,
)
from atlas.text import find_personal_data, hidden_characters, published_form

if TYPE_CHECKING:
    from atlas.geo.counties import CountyIndex

RULES = (
    "file",
    "schema",
    "jsonschema",
    "rollup",
    "dates",
    "sources",
    "support",
    "quote",
    "merged_into",
    "geo",
    "range",
    "privacy",
    "text",
    "org",
    "phase",
    "scope",
)

# Precisions that are plotted at a coordinate, and those that must have none (07 §2.4).
POINT_PRECISIONS = frozenset(
    {"footprint", "parcel", "site", "address", "street", "locality", "county"}
)
NO_POINT_PRECISIONS = frozenset({"state", "unknown"})
# Precisions whose point must fall inside the stated county polygon (rule 5).
COUNTY_CHECKED_PRECISIONS = frozenset(
    {"footprint", "parcel", "site", "address", "street", "county"}
)

MW_MAX = 10_000.0
INVESTMENT_MAX_USD = 5e11
ACREAGE_MAX = 100_000.0
WATER_MAX_MGD = 50.0
EARLIEST_DATE = date(1990, 1, 1)
FUTURE_YEARS = 15

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Identifier strings: parcel numbers, queue ids and external ids are dashed digit runs that the
# phone pattern would flag (a Cook County PIN, 08-35-302-012-0000), so they get the email check only.
_IDENTIFIER_RE = re.compile(
    r"^/(?:location/parcel_apns/\d+|location/geometry_ref|grid/queue_ids/\d+|external_ids(?:/.*)?"
    r"|buildings/\d+/ref)$"
)
# Strings that other values refer to by exact text.
_REFERENCE_RE = re.compile(r"^/(?:.*/)?(?:phase_id|org_id|source_ids/\d+)$")
# Control characters (Cc) and line or paragraph separators: never part of a stored string.
_CONTROL_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
# Characters escaped in Issue.format, so record text cannot start a new output line (a GitHub
# Actions workflow command such as "::error::") or reorder the line on screen.
_UNPRINTABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_ORGS = TypeAdapter(list[Org])
_PARTY_ROLES = ("operator", "owner", "developer", "tenant", "filing_entities")


@dataclass(frozen=True)
class Issue:
    rule: str
    message: str
    record_id: str | None = None
    file: str | None = None
    pointer: str | None = None

    def format(self) -> str:
        """`{file}:{pointer} [{rule}] {message}` on one line; the record id stands in for a missing
        file. Control, format and separator characters are written as \\uXXXX escapes."""
        where = self.file or self.record_id or "-"
        line = _escape_unprintable(f"{where}:{self.pointer or ''} [{self.rule}] {self.message}")
        return "\\u003a" + line[1:] if line.startswith("::") else line

    def to_json(self) -> dict[str, str | None]:
        return {
            "rule": self.rule,
            "message": self.message,
            "record_id": self.record_id,
            "file": self.file,
            "pointer": self.pointer,
        }


def _escape_unprintable(s: str) -> str:
    if s.isprintable():
        return s
    return "".join(
        (f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
        if unicodedata.category(ch) in _UNPRINTABLE_CATEGORIES
        else ch
        for ch in s
    )


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    records: int = 0
    in_scope: int = 0
    out_of_scope: int = 0
    merged: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def loc_to_pointer(loc: tuple[int | str, ...]) -> str:
    return "".join(f"/{escape(str(part))}" for part in loc)


# ---------------------------------------------------------------------------- per-record rules

Add = Callable[..., None]


def _check_rollup_and_dates(record: FacilityRecord, add: Add) -> None:
    try:
        expected = rollup_status(record)
    except RollupError:
        add("rollup", "status_history has no non-planned event", "/status_history")
        return
    if record.status != expected:
        add("rollup", f"status is {record.status}, but the events roll up to {expected}", "/status")
    derived = derive_dates(record)
    for key in DERIVED_DATE_KEYS:
        have, want = record.dates.get(key), derived.get(key)
        if have != want:
            add(
                "dates",
                f"dates.{key} is {_fmt_date(have)}, but the events give {_fmt_date(want)}",
                f"/dates/{key}",
            )


def _fmt_date(d: FuzzyDate | None) -> str:
    return "absent" if d is None else f"{d.value} ({d.precision})"


def _source_refs(value: Any, pointer: str) -> Iterator[tuple[str, str]]:
    """(pointer, source id) for every source_ids entry, including field_meta conflicts."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            child = getattr(value, name)
            child_ptr = f"{pointer}/{escape(name)}"
            if name == "source_ids" and isinstance(child, list):
                for i, sid in enumerate(child):
                    yield f"{child_ptr}/{i}", str(sid)
            elif name == "conflicts" and isinstance(child, list):
                for i, conflict in enumerate(child):
                    ids = conflict.get("source_ids") if isinstance(conflict, dict) else None
                    if isinstance(ids, list):
                        for j, sid in enumerate(ids):
                            yield f"{child_ptr}/{i}/source_ids/{j}", str(sid)
            else:
                yield from _source_refs(child, child_ptr)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _source_refs(item, f"{pointer}/{i}")
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _source_refs(value[key], f"{pointer}/{escape(str(key))}")


def _check_sources(record: FacilityRecord, add: Add) -> None:
    ids = [s.id for s in record.sources]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        add("sources", f"duplicate source id {dup}", "/sources")
    known = set(ids)
    for pointer, sid in _source_refs(record, ""):
        if sid not in known:
            add("sources", f"source id {sid} does not exist in sources", pointer)
    doc = record_json(record)
    for i, source in enumerate(record.sources):
        for j, entry in enumerate(source.supports):
            try:
                found = entry != "" and resolve(doc, entry)[0]
            except ValueError:
                found = False
            if not found:
                add(
                    "sources",
                    f"supports entry {entry!r} is not a path in the record",
                    f"/sources/{i}/supports/{j}",
                )


def _check_support(record: FacilityRecord, add: Add) -> None:
    for pointer in unsupported_pointers(record):
        add("support", "no source supports this value (07 §3.3)", pointer)


def _check_quotes(record: FacilityRecord, add: Add) -> None:
    for i, s in enumerate(record.sources):
        base = f"/sources/{i}"
        if s.text_sha256 is not None and not _SHA256_RE.match(s.text_sha256):
            add("quote", "text_sha256 is not 64 lowercase hex characters", f"{base}/text_sha256")
        if s.quote_start is None and s.quote_end is None:
            continue
        if s.quote_start is None or s.quote_end is None:
            add("quote", "quote_start and quote_end must be set together", base)
        elif not 0 <= s.quote_start < s.quote_end:
            add(
                "quote",
                f"quote offsets {s.quote_start}..{s.quote_end} are not 0 <= start < end",
                f"{base}/quote_start",
            )
        if not s.text_sha256:
            add("quote", "quote offsets need text_sha256", f"{base}/text_sha256")
        if not s.quote:
            add("quote", "quote offsets need the quote", f"{base}/quote")


def _check_geo(record: FacilityRecord, counties: CountyIndex | None, add: Add) -> None:
    loc = record.location
    has_lat, has_lon = loc.lat is not None, loc.lon is not None
    if has_lat != has_lon:
        add("geo", "lat and lon must both be set or both be null", "/location")
    if loc.precision in POINT_PRECISIONS and not (has_lat and has_lon):
        add("geo", f"precision {loc.precision} needs lat and lon", "/location/precision")
    if loc.precision in NO_POINT_PRECISIONS and (has_lat or has_lon):
        add("geo", f"precision {loc.precision} must not have coordinates", "/location/precision")
    state = state_by_abbr(loc.state_abbr)
    if state is None:
        add("geo", f"unknown state_abbr {loc.state_abbr}", "/location/state_abbr")
    elif loc.county_fips is not None and loc.county_fips[:2] != state.fips:
        add(
            "geo",
            f"county_fips {loc.county_fips} is not in {loc.state_abbr} (FIPS {state.fips})",
            "/location/county_fips",
        )
    checked = loc.precision in COUNTY_CHECKED_PRECISIONS
    if checked and loc.county_fips is None:
        add("geo", f"precision {loc.precision} needs county_fips", "/location/county_fips")
    if counties is None:
        return
    if loc.county_fips is not None and counties.get(loc.county_fips) is None:
        add(
            "geo",
            f"county_fips {loc.county_fips} is not a 2025 Census county",
            "/location/county_fips",
        )
        return
    if loc.lat is None or loc.lon is None:
        return
    if checked and loc.county_fips is not None:
        if not counties.contains(loc.county_fips, loc.lat, loc.lon):
            add(
                "geo",
                f"({loc.lat}, {loc.lon}) is not inside county {loc.county_fips}",
                "/location",
            )
    elif loc.precision == "locality" and not counties.in_state(loc.state_abbr, loc.lat, loc.lon):
        add("geo", f"({loc.lat}, {loc.lon}) is not inside {loc.state_abbr}", "/location")


def _check_ranges(record: FacilityRecord, today: date, add: Add) -> None:
    capacities = [("/capacity", record.capacity)] + [
        (f"/phases/{i}/capacity", p.capacity) for i, p in enumerate(record.phases)
    ]
    for base, cap in capacities:
        for name in ("it_mw", "facility_mw", "utility_request_mw", "backup_generation_mw"):
            value = getattr(cap, name)
            if value is not None and not 0 < value <= MW_MAX:
                add("range", f"{name} {value} is outside (0, {MW_MAX:g}]", f"{base}/{name}")
    usd = record.money.investment_usd
    if usd is not None and not 0 < usd <= INVESTMENT_MAX_USD:
        add("range", f"investment_usd {usd:g} is outside (0, 5e11]", "/money/investment_usd")
    acres = record.site.acreage
    if acres is not None and not 0 < acres <= ACREAGE_MAX:
        add("range", f"acreage {acres:g} is outside (0, {ACREAGE_MAX:g}]", "/site/acreage")
    water = record.cooling.water_use_mgd
    if water is not None and not 0 <= water <= WATER_MAX_MGD:
        add(
            "range",
            f"water_use_mgd {water:g} is outside [0, {WATER_MAX_MGD:g}]",
            "/cooling/water_use_mgd",
        )
    latest = add_years(today, FUTURE_YEARS)
    dates: list[tuple[str, FuzzyDate]] = [
        (f"/status_history/{i}/as_of", e.as_of) for i, e in enumerate(record.status_history)
    ]
    dates += [(f"/dates/{escape(k)}", v) for k, v in sorted(record.dates.items())]
    dates += [
        (f"/phases/{i}/expected_in_service", p.expected_in_service)
        for i, p in enumerate(record.phases)
        if p.expected_in_service is not None
    ]
    for pointer, d in dates:
        start = period_start(d)
        if not EARLIEST_DATE <= start <= latest:
            add("range", f"date {d.value} is outside {EARLIEST_DATE} to {latest}", pointer)


def _strings(value: Any, pointer: str) -> Iterator[tuple[str, str]]:
    """(pointer, text) for every string value and dict key; URLs and dates are not strings."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _strings(getattr(value, name), f"{pointer}/{escape(name)}")
    elif isinstance(value, str):
        yield pointer, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{pointer}/{i}")
    elif isinstance(value, dict):
        for key in sorted(value, key=str):
            child = f"{pointer}/{escape(str(key))}"
            if isinstance(key, str):
                yield child, key
            yield from _strings(value[key], child)


def _check_privacy(record: FacilityRecord, add: Add) -> None:
    """Rule 7 on the text publish emits (invisible characters removed), in NFKC form."""
    for pointer, text in _strings(record, ""):
        phones = not _IDENTIFIER_RE.match(pointer)
        found = find_personal_data(published_form(text), phones=phones)
        if found:
            add(
                "privacy",
                f"looks like an email address or phone number ({len(found)} match(es)); "
                "records hold organizations only (07 §5.3)",
                pointer,
            )


def _codepoints(chars: list[str]) -> str:
    shown = ", ".join(dict.fromkeys(f"U+{ord(ch):04X}" for ch in chars))
    return f"{len(chars)} ({shown})"


def _check_text(record: FacilityRecord, add: Add) -> None:
    """No control characters in any string, so record text cannot break an output line, and no
    invisible characters in identifiers and references, which publish's strip_invisible would
    otherwise change (other text is published without them, and rule 7 checks that form)."""
    for pointer, text in _strings(record, ""):
        bad = [ch for ch in text if unicodedata.category(ch) in _CONTROL_CATEGORIES]
        if _IDENTIFIER_RE.match(pointer) or _REFERENCE_RE.match(pointer):
            bad += hidden_characters(text)
        if bad:
            add(
                "text",
                f"has control or invisible characters: {_codepoints(bad)}; remove them",
                pointer,
            )


def _check_phases(record: FacilityRecord, add: Add) -> None:
    ids = [p.phase_id for p in record.phases]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        add("phase", f"duplicate phase_id {dup}", "/phases")
    known = set(ids)
    for i, e in enumerate(record.status_history):
        if e.phase_id is not None and e.phase_id not in known:
            add("phase", f"phase_id {e.phase_id} is not in phases", f"/status_history/{i}/phase_id")
    for i, b in enumerate(record.buildings):
        if b.phase_id is not None and b.phase_id not in known:
            add("phase", f"phase_id {b.phase_id} is not in phases", f"/buildings/{i}/phase_id")


def _check_scope(record: FacilityRecord, add: Add) -> None:
    if record.scope == "in_scope" and record.location.state_abbr not in IN_SCOPE:
        add(
            "scope",
            f"{record.location.state_abbr} is outside the 50 states and DC; "
            'set scope to "out_of_scope" (07 §2.2)',
            "/scope",
        )


def validate_record(
    record: FacilityRecord, *, counties: CountyIndex | None, today: date
) -> list[Issue]:
    """The per-record rules: rollup, dates, sources, support, quote, geo, range, privacy, text,
    phase and scope. counties=None skips the polygon checks."""
    issues: list[Issue] = []

    def add(rule: str, message: str, pointer: str | None = None) -> None:
        issues.append(Issue(rule, message, record.id, None, pointer))

    _check_rollup_and_dates(record, add)
    _check_sources(record, add)
    _check_support(record, add)
    _check_quotes(record, add)
    _check_geo(record, counties, add)
    _check_ranges(record, today, add)
    _check_privacy(record, add)
    _check_text(record, add)
    _check_phases(record, add)
    _check_scope(record, add)
    return issues


# ---------------------------------------------------------------------------- dataset rules


def _load_schema_validator(schema_path: Path, issues: list[Issue]) -> Any:
    from jsonschema import Draft202012Validator  # imported on use
    from jsonschema.exceptions import SchemaError

    try:
        text = schema_path.read_text(encoding="utf-8")
    except OSError as e:
        issues.append(
            Issue("jsonschema", f"cannot read the JSON Schema: {e}", file=str(schema_path))
        )
        return None
    if text != render():
        issues.append(
            Issue(
                "jsonschema",
                f"{schema_path} is out of date; run uv run atlas schema export",
                file=str(schema_path),
            )
        )
    try:
        schema: Any = loads(text)
        Draft202012Validator.check_schema(schema)
    except (ValueError, SchemaError) as e:
        issues.append(Issue("jsonschema", f"not a valid JSON Schema: {e}", file=str(schema_path)))
        return None
    return Draft202012Validator(schema)


def _load_orgs(orgs_path: Path, issues: list[Issue]) -> list[Org]:
    where = str(orgs_path)
    try:
        text = orgs_path.read_bytes().decode("utf-8")
        raw = loads(text)
    except (OSError, ValueError) as e:
        issues.append(Issue("org", f"cannot read orgs: {e}", file=where))
        return []
    try:
        orgs = _ORGS.validate_python(raw)
    except ValidationError as e:
        for err in e.errors():
            issues.append(Issue("org", err["msg"], file=where, pointer=loc_to_pointer(err["loc"])))
        return []
    ids = [o.id for o in orgs]
    if ids != sorted(ids):
        issues.append(Issue("org", "orgs must be sorted by id", file=where))
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        issues.append(Issue("org", f"duplicate org id {dup}", file=where))
    known = set(ids)
    for i, org in enumerate(orgs):
        if org.parent_id is not None and org.parent_id not in known:
            issues.append(
                Issue(
                    "org",
                    f"parent_id {org.parent_id} is not in orgs",
                    file=where,
                    pointer=f"/{i}/parent_id",
                )
            )
    if text != dumps_pretty([o.model_dump(mode="json") for o in orgs]):
        issues.append(Issue("file", "orgs file is not in canonical form", file=where))
    return orgs


def _validate_file(
    path: Path,
    validator: Any,
    counties: CountyIndex,
    today: date,
    issues: list[Issue],
) -> FacilityRecord | None:
    where = str(path)
    try:
        text = path.read_bytes().decode("utf-8")
        doc = loads(text)
    except (OSError, ValueError) as e:
        issues.append(Issue("file", f"not a UTF-8 JSON file: {e}", file=where))
        return None
    if validator is not None:
        errors = sorted(
            validator.iter_errors(doc),
            key=lambda err: ([str(p) for p in err.absolute_path], err.message),
        )
        for err in errors:
            issues.append(
                Issue(
                    "jsonschema",
                    err.message,
                    file=where,
                    pointer=loc_to_pointer(tuple(err.absolute_path)),
                )
            )
    try:
        record = FacilityRecord.model_validate(doc)
    except ValidationError as e:
        record_id = doc.get("id") if isinstance(doc, dict) else None
        for err in e.errors():
            issues.append(
                Issue(
                    "schema",
                    err["msg"],
                    record_id=record_id if isinstance(record_id, str) else None,
                    file=where,
                    pointer=loc_to_pointer(err["loc"]),
                )
            )
        return None
    if path.name != f"{record.id}.json":
        issues.append(Issue("file", f"file name must be {record.id}.json", record.id, where))
    if text != dumps_pretty(record_json(record)):
        issues.append(
            Issue(
                "file",
                "not in canonical form (sorted keys, 2-space indent, LF, every key present); "
                "rewrite it with RecordStore.write",
                record.id,
                where,
            )
        )
    for issue in validate_record(record, counties=counties, today=today):
        issues.append(replace(issue, file=where))
    return record


def validate_dataset(
    records_dir: Path,
    orgs_path: Path,
    *,
    schema_path: Path,
    counties: CountyIndex,
    today: date,
) -> ValidationReport:
    """Every rule over a records directory, its orgs file and the JSON Schema."""
    report = ValidationReport()
    issues = report.issues
    validator = _load_schema_validator(schema_path, issues)
    orgs = _load_orgs(orgs_path, issues)
    if not records_dir.is_dir():
        issues.append(Issue("file", "records directory not found", file=str(records_dir)))
        return report
    records: dict[str, FacilityRecord] = {}
    files: dict[str, str] = {}
    for path in sorted(records_dir.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_dir() or path.suffix != ".json":
            issues.append(Issue("file", "only {id}.json files belong here", file=str(path)))
            continue
        record = _validate_file(path, validator, counties, today, issues)
        if record is None:
            continue
        if record.id in records:
            issues.append(
                Issue("file", f"duplicate id (also in {files[record.id]})", record.id, str(path))
            )
            continue
        records[record.id] = record
        files[record.id] = str(path)

    org_ids = {o.id for o in orgs}
    for rid, record in records.items():
        where = files[rid]
        if record.merged_into is not None:
            target = records.get(record.merged_into)
            if target is None:
                issues.append(
                    Issue(
                        "merged_into",
                        f"target {record.merged_into} does not exist",
                        rid,
                        where,
                        "/merged_into",
                    )
                )
            elif target.merged_into is not None:
                issues.append(
                    Issue(
                        "merged_into",
                        f"target {record.merged_into} is itself merged into {target.merged_into}",
                        rid,
                        where,
                        "/merged_into",
                    )
                )
        for role in _PARTY_ROLES:
            for i, ref in enumerate(getattr(record.parties, role)):
                if ref.org_id is not None and ref.org_id not in org_ids:
                    issues.append(
                        Issue(
                            "org",
                            f"org_id {ref.org_id} is not in {orgs_path}",
                            rid,
                            where,
                            f"/parties/{role}/{i}/org_id",
                        )
                    )
        if record.merged_into is not None:
            report.merged += 1
        elif record.scope == "out_of_scope":
            report.out_of_scope += 1
        else:
            report.in_scope += 1
    report.records = len(records)
    return report
