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


def from_osm_tags(tags: Mapping[str, str]) -> Crosswalked | None:
    """OSM tags, most specific first: proposed, then construction, then operating (assumed).

    building=construction + construction=data_center (about 30 US objects) counts as under
    construction even when telecom=data_center is also set.
    """
    if tags.get("proposed:telecom") == "data_center":
        return Crosswalked(
            "proposed", "first_reported", None, None, OSM_CONFIDENCE, "proposed:telecom=data_center"
        )
    if tags.get("construction:telecom") == "data_center":
        return Crosswalked(
            "under_construction",
            "first_reported",
            None,
            None,
            OSM_CONFIDENCE,
            "construction:telecom=data_center",
        )
    if tags.get("construction") == "data_center":
        return Crosswalked(
            "under_construction",
            "first_reported",
            None,
            None,
            OSM_CONFIDENCE,
            "construction=data_center",
        )
    if tags.get("telecom") == "data_center":
        return Crosswalked(
            "operating", "first_reported", None, None, OSM_CONFIDENCE, "telecom=data_center"
        )
    if tags.get("building") == "data_center":
        return Crosswalked(
            "operating", "first_reported", None, None, OSM_CONFIDENCE, "building=data_center"
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
    "withdrawn": ("cancelled", "withdrawn", None, "developer_withdrawal"),
}


def from_aigridwatch_stage(stage: str, *, has_filing: bool) -> Crosswalked:
    """An AI GridWatch stage. "Proposed" means announced unless a filing is known."""
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


def from_epoch_row(construction_status: str, buildings_operational: float | None) -> Crosswalked:
    """An Epoch timeline row: operating when any building is operational; announced when the text
    is only about plans; under construction otherwise."""
    if buildings_operational is not None and buildings_operational > 0:
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
