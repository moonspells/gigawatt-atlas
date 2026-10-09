"""Status crosswalk from upstream values to Atlas statuses and events (07 §4.7).

docs/status-crosswalk.md is this module as a table. PA DEP and ECHO mappings come at M4.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from atlas.schema.record import EventType, EvidenceLevel, Status, StatusReason

OSM_CONFIDENCE = 0.60
DATASET_CONFIDENCE = 0.70

EPOCH_CONSTRUCTION = re.compile(
    r"\b(land clearing|clearing|grading|civil works|construction|foundation|groundbreaking|"
    r"ground broken|site work|excavat\w*|steel|roof|built|conversion|retrofit\w*|installed|"
    r"complete[d]?)\b",
    re.I,
)
EPOCH_PRE_CONSTRUCTION = re.compile(
    r"\b(announc\w*|planned|proposed|permit application|rezoning|acquired|purchased)\b", re.I
)


@dataclass(frozen=True)
class Crosswalked:
    status: Status
    event: EventType
    evidence_level: EvidenceLevel | None
    status_reason: StatusReason | None
    confidence: float
    label: str  # the upstream value, as given


class UnknownStatus(ValueError):  # noqa: N818  (name fixed by the interface)
    """An upstream status value the crosswalk does not know."""


# Keys whose value "data_center" makes an OSM object a data center (the Overpass query fetches the
# first five).
_OSM_DATA_CENTER_KEYS = (
    "telecom",
    "building",
    "construction:telecom",
    "proposed:telecom",
    "construction",
    "construction:building",
    "proposed:building",
)
# Lifecycle tags, most specific first. A tag listed with a value matches only that value; one
# listed with None matches any value except "no".
_OSM_PROPOSED_TAGS: tuple[tuple[str, str | None], ...] = (
    ("proposed:telecom", "data_center"),
    ("proposed:building", None),
    ("proposed", None),
)
_OSM_CONSTRUCTION_TAGS: tuple[tuple[str, str | None], ...] = (
    ("construction:telecom", "data_center"),
    ("construction", "data_center"),
    ("construction:building", None),
    ("building", "construction"),
    ("landuse", "construction"),
)


def _osm_match(tags: Mapping[str, str], rules: tuple[tuple[str, str | None], ...]) -> str | None:
    for key, wanted in rules:
        value = tags.get(key)
        if value is None or value == "no":
            continue
        if wanted is None or value == wanted:
            return f"{key}={value}"
    return None


def from_osm_tags(tags: Mapping[str, str]) -> Crosswalked | None:
    """OSM tags of a data center, lifecycle first: proposed, then under construction, then
    operating (assumed). None for an object no data_center tag marks as a data center.

    Proposed: proposed:telecom=data_center, proposed:building=* or proposed=*. Under
    construction: construction:telecom=data_center, construction=data_center,
    construction:building=*, building=construction or landuse=construction. A lifecycle tag wins
    over telecom=data_center, so a site OSM tags as planned or being built is never operating.
    """
    if not any(tags.get(key) == "data_center" for key in _OSM_DATA_CENTER_KEYS):
        return None
    label = _osm_match(tags, _OSM_PROPOSED_TAGS)
    if label is not None:
        return Crosswalked("proposed", "first_reported", None, None, OSM_CONFIDENCE, label)
    label = _osm_match(tags, _OSM_CONSTRUCTION_TAGS)
    if label is not None:
        return Crosswalked(
            "under_construction", "first_reported", None, None, OSM_CONFIDENCE, label
        )
    for key in ("telecom", "building"):
        if tags.get(key) == "data_center":
            return Crosswalked(
                "operating", "first_reported", None, None, OSM_CONFIDENCE, f"{key}=data_center"
            )
    return None


def _key(s: str) -> str:
    return " ".join(s.split()).casefold()


_AGW_FIXED: dict[str, tuple[Status, EventType, EvidenceLevel | None, StatusReason | None]] = {
    "rumored": ("announced", "announced", "rumor", None),
    "in review": ("proposed", "application_filed", None, None),
    "hearing scheduled": ("proposed", "application_filed", None, None),
    "awaiting decision": ("proposed", "application_filed", None, None),
    "approved": ("permitted", "approved", None, None),
    "under construction": ("under_construction", "construction_start", None, None),
    "operating": ("operating", "energized", None, None),
    "blocked by ban": ("paused", "paused", None, "moratorium"),
    "denied": ("denied", "denied", None, "local_denial"),
    # Who withdrew is not in the stage: a developer, a mayor dropping his support, a lapsed host
    # agreement or a court ruling all read "Withdrawn". The importer sets the reason from the row.
    "withdrawn": ("cancelled", "withdrawn", None, None),
}


def from_aigridwatch_stage(stage: str, *, has_filing: bool) -> Crosswalked:
    """An AI GridWatch stage. "Proposed" means announced unless a filing is known; "Withdrawn"
    carries no status_reason, since the stage does not say who withdrew."""
    key = _key(stage)
    if key == "proposed":
        if has_filing:
            return Crosswalked(
                "proposed", "application_filed", None, None, DATASET_CONFIDENCE, stage
            )
        return Crosswalked("announced", "announced", None, None, DATASET_CONFIDENCE, stage)
    if key not in _AGW_FIXED:
        raise UnknownStatus(f"unknown AI GridWatch stage: {stage!r}")
    status, event, evidence, reason = _AGW_FIXED[key]
    return Crosswalked(status, event, evidence, reason, DATASET_CONFIDENCE, stage)


def from_epoch_row(
    construction_status: str, buildings_operational: float | None, *, it_mw: float | None = None
) -> Crosswalked:
    """An Epoch timeline row: operating when any building is operational (or, when the building
    count is empty, when the row's IT power is above 0); announced when the text is only about
    plans; under construction otherwise.

    In Epoch's timelines IT power is the power online at that date: every row with buildings
    operational > 0 has IT power > 0, and every row with 0 has 0 (547 rows, 2026-10-07).
    """
    operational = buildings_operational if buildings_operational is not None else it_mw
    if operational is not None and operational > 0:
        return Crosswalked(
            "operating", "energized", None, None, DATASET_CONFIDENCE, construction_status
        )
    if EPOCH_PRE_CONSTRUCTION.search(construction_status) and not EPOCH_CONSTRUCTION.search(
        construction_status
    ):
        return Crosswalked(
            "announced", "announced", None, None, DATASET_CONFIDENCE, construction_status
        )
    return Crosswalked(
        "under_construction",
        "construction_start",
        None,
        None,
        DATASET_CONFIDENCE,
        construction_status,
    )
