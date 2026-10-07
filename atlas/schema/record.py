"""The canonical facility record, schema_version 1.0.0 (07 §3.2).

Field names, types, defaults and constraints follow 07 §3.2. Deviations, all deliberate:

- Every model inherits AtlasModel: unknown keys are rejected, and the serialization JSON Schema
  marks fields with defaults as required, because record files are always the full dump.
- FuzzyDate checks that the value's shape matches its precision and that a day is a real date.
- FieldMeta.conflicts is list[dict[str, JsonValue]], and Correction.old/new are JsonValue | None,
  because §3.2's `dict` and `object` have no JSON Schema.
- Mutable defaults use Field(default_factory=...).

Serialization is always record.model_dump(mode="json") (atlas.jsonio.record_json): nothing is
excluded and every key is present. HttpUrl normalizes a bare host with a trailing slash
(https://aigridwatch.com -> https://aigridwatch.com/), and that normalized form is what is stored.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    model_validator,
)

SCHEMA_VERSION = "1.0.0"
RECORD_ID_PATTERN = r"^gwa-[0-9a-hjkmnp-tv-z]{26}$"
ORG_ID_PATTERN = r"^gwo-[0-9a-hjkmnp-tv-z]{26}$"
# Importers build candidates with this id; atlas.sources.base.apply_import replaces it.
PLACEHOLDER_ID = "gwa-" + "0" * 26
FUZZY_DATE_PATTERN = r"^\d{4}(-Q[1-4]|-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$"

Status = Literal[
    "announced",
    "proposed",
    "permitted",
    "under_construction",
    "operating",
    "paused",
    "denied",
    "cancelled",
]
EventType = Literal[
    "first_reported",
    "announced",
    "application_filed",
    "hearing_scheduled",
    "hearing_held",
    "approved",
    "permit_issued",
    "denied",
    "construction_start",
    "energized",
    "phase_online",
    "paused",
    "resumed",
    "withdrawn",
    "cancelled",
    "correction",
    "other",
]
Precision = Literal[
    "footprint", "parcel", "site", "address", "street", "locality", "county", "state", "unknown"
]
SourceType = Literal[
    "government_record",
    "utility_or_iso_filing",
    "court_or_regulator",
    "sec_filing",
    "company_release",
    "trade_press",
    "news",
    "open_dataset",
    "commercial_aggregator",
    "community_report",
]
EvidenceLevel = Literal["confirmed", "reported", "rumor"]
StatusReason = Literal[
    "regulatory_pause", "local_denial", "developer_withdrawal", "moratorium", "litigation", "other"
]
GeocodeMethod = Literal[
    "source_coords", "osm", "census_geocoder", "gazetteer", "county_centroid", "manual"
]
DatePrecision = Literal["day", "month", "quarter", "year"]

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_QUARTER_RE = re.compile(r"^\d{4}-Q[1-4]$")
_YEAR_RE = re.compile(r"^\d{4}$")


def _shape_precision(value: str) -> DatePrecision:
    if _DAY_RE.match(value):
        return "day"
    if _MONTH_RE.match(value):
        return "month"
    if _QUARTER_RE.match(value):
        return "quarter"
    if _YEAR_RE.match(value):
        return "year"
    raise ValueError(f"not a fuzzy date: {value!r}")


class AtlasModel(BaseModel):
    """Base for every Atlas model: no unknown keys; defaults are required in the JSON Schema."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class FuzzyDate(AtlasModel):
    """A date known to a year, quarter, month or day: YYYY | YYYY-Qn | YYYY-MM | YYYY-MM-DD."""

    value: str = Field(pattern=FUZZY_DATE_PATTERN)
    precision: DatePrecision

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        shape = _shape_precision(self.value)
        if shape != self.precision:
            raise ValueError(
                f"precision {self.precision!r} does not match {self.value!r} (a {shape} value)"
            )
        if shape == "day":
            date.fromisoformat(self.value)  # raises ValueError for 2026-02-30
        return self


class StatusEvent(AtlasModel):
    """One dated event in status_history. Planned events never change the current status."""

    seq: int
    status: Status
    event: EventType
    as_of: FuzzyDate
    phase_id: str | None = None
    planned: bool = False
    source_ids: list[str] = Field(min_length=1)
    note: str | None = Field(None, max_length=200)


class OrgRef(AtlasModel):
    """An organization as written in a source, optionally resolved to data/orgs.json."""

    name: str
    org_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)


class Parties(AtlasModel):
    """Operator, owner, developer, tenant and the filing entities (shell LLCs) of a facility."""

    operator: list[OrgRef] = Field(default_factory=list)
    owner: list[OrgRef] = Field(default_factory=list)
    developer: list[OrgRef] = Field(default_factory=list)
    tenant: list[OrgRef] = Field(default_factory=list)
    filing_entities: list[OrgRef] = Field(default_factory=list)


class Alias(AtlasModel):
    """Another name for the facility."""

    name: str
    kind: Literal[
        "codename",
        "press_name",
        "permit_name",
        "filing_llc",
        "osm_name",
        "former_name",
        "phase_name",
    ]
    source_ids: list[str] = Field(default_factory=list)


class Location(AtlasModel):
    """Where the facility is, and how precisely that is known (07 §2.4)."""

    lat: float | None = Field(None, ge=18, le=72)
    lon: float | None = Field(None, ge=-180, le=-64)
    precision: Precision
    geometry_ref: str | None = None
    street: str | None = None
    city: str | None = None
    postcode: str | None = None
    municipality: str | None = None
    county_name: str | None = None
    county_fips: str | None = Field(None, pattern=r"^\d{5}$")
    state_abbr: str = Field(pattern=r"^[A-Z]{2}$")
    parcel_apns: list[str] = Field(default_factory=list)
    geocode_method: GeocodeMethod | None = None


class Capacity(AtlasModel):
    """Power in MW. The bases are never mixed or converted (07 §2.4)."""

    it_mw: float | None = None
    facility_mw: float | None = None
    utility_request_mw: float | None = None
    backup_generation_mw: float | None = None
    mw_as_stated: str | None = None


class Phase(AtlasModel):
    """A phase of a campus or project; events refer to it by phase_id."""

    phase_id: str
    name: str
    capacity: Capacity = Field(default_factory=Capacity)
    building_sqft: float | None = None
    expected_in_service: FuzzyDate | None = None
    source_ids: list[str] = Field(default_factory=list)


class Building(AtlasModel):
    """One building, such as an OSM way or a TDLR TABS registration."""

    ref: str
    name: str | None = None
    sqft: float | None = None
    phase_id: str | None = None


class Site(AtlasModel):
    """Land and floor area."""

    acreage: float | None = None
    building_sqft: float | None = None
    building_count: int | None = None


class Money(AtlasModel):
    """Investment as stated, with its basis."""

    investment_usd: float | None = None
    investment_basis: (
        Literal[
            "announced_total",
            "permit_construction_estimate",
            "incentive_agreement_minimum",
            "filing",
            "estimate",
        ]
        | None
    ) = None
    currency_year: int | None = None


class Grid(AtlasModel):
    """Utility, ISO/RTO and interconnection queue ids."""

    utility_name: str | None = None
    utility_eia_id: int | None = None
    iso_rto: Literal[
        "ERCOT",
        "PJM",
        "MISO",
        "SPP",
        "CAISO",
        "NYISO",
        "ISO-NE",
        "non-RTO-West",
        "non-RTO-Southeast",
        "unknown",
    ] = "unknown"
    balancing_authority: str | None = None
    queue_ids: list[str] = Field(default_factory=list)


class Cooling(AtlasModel):
    """Cooling type and water."""

    type: Literal[
        "air",
        "evaporative",
        "closed_loop_liquid",
        "direct_to_chip",
        "immersion",
        "hybrid",
        "unknown",
    ] = "unknown"
    water_source: Literal[
        "municipal", "groundwater", "surface", "reclaimed", "none_claimed", "unknown"
    ] = "unknown"
    water_use_mgd: float | None = None


class Incentive(AtlasModel):
    """A tax or utility incentive tied to the facility."""

    program: str
    jurisdiction: str
    kind: Literal[
        "sales_tax_exemption", "property_tax_abatement", "pilot", "grant", "utility_rate", "other"
    ]
    value_usd: float | None = None
    term_years: int | None = None
    status: Literal["proposed", "approved", "denied", "unknown"]
    source_ids: list[str] = Field(min_length=1)


class Source(AtlasModel):
    """A cited source. supports lists the JSON Pointers it backs (07 §3.3)."""

    id: str = Field(pattern=r"^s\d+$")
    url: HttpUrl
    archive_url: HttpUrl | None = None
    publisher: str
    title: str | None = None
    source_type: SourceType
    license: str | None = None
    published_at: AwareDatetime | None = None
    retrieved_at: AwareDatetime
    text_sha256: str | None = None
    quote: str | None = Field(None, max_length=300)
    quote_start: int | None = None
    quote_end: int | None = None
    quote_match: Literal["exact", "fuzzy", "human"] | None = None
    supports: list[str] = Field(default_factory=list)


class FieldMeta(AtlasModel):
    """Confidence and provenance of one field, keyed by JSON Pointer in field_meta (07 §3.4)."""

    confidence: float = Field(ge=0, le=1)
    method: Literal["stated", "inferred", "derived", "imported", "human_entered"]
    source_ids: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, JsonValue]] = Field(default_factory=list)


class Review(AtlasModel):
    """Whether a person has reviewed the record."""

    state: Literal["machine", "human_reviewed", "disputed"]
    pr_url: HttpUrl | None = None
    reviewed_at: AwareDatetime | None = None


class Correction(AtlasModel):
    """An accepted correction (07 §5.4)."""

    on: date
    pointer: str
    old: JsonValue | None
    new: JsonValue | None
    reason: str = Field(max_length=300)
    issue_url: HttpUrl | None = None


class FacilityRecord(AtlasModel):
    """A campus or project: one map point and one count (07 §2.1)."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    id: str = Field(pattern=RECORD_ID_PATTERN)
    record_type: Literal["campus", "project"]
    parent_id: None = None
    merged_into: str | None = None
    scope: Literal["in_scope", "out_of_scope"] = "in_scope"
    canonical_name: str
    aliases: list[Alias] = Field(default_factory=list)
    parties: Parties = Field(default_factory=Parties)
    purpose: Literal[
        "ai_training",
        "ai_inference",
        "hyperscale_cloud",
        "colocation",
        "enterprise",
        "crypto_hpc",
        "telecom",
        "mixed",
        "unknown",
    ] = "unknown"
    status: Status
    status_reason: StatusReason | None = None
    evidence_level: EvidenceLevel
    status_history: list[StatusEvent] = Field(min_length=1)
    location: Location
    capacity: Capacity = Field(default_factory=Capacity)
    phases: list[Phase] = Field(default_factory=list)
    buildings: list[Building] = Field(default_factory=list)
    site: Site = Field(default_factory=Site)
    money: Money = Field(default_factory=Money)
    dates: dict[str, FuzzyDate] = Field(default_factory=dict)
    grid: Grid = Field(default_factory=Grid)
    cooling: Cooling = Field(default_factory=Cooling)
    incentives: list[Incentive] = Field(default_factory=list)
    external_ids: dict[str, list[str]] = Field(default_factory=dict)
    sources: list[Source] = Field(min_length=1)
    field_meta: dict[str, FieldMeta] = Field(default_factory=dict)
    review: Review
    corrections: list[Correction] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_verified_at: AwareDatetime
