"""Status rollup, derived dates and display helpers (07 §2.3, §2.4).

Only non-planned events count. Events are ordered by event_key: the first day of their as_of
period, then seq. A record without phases takes the status of its latest event. A record with
phases takes the most advanced active phase status; a stopped status only when every phase with an
event is stopped.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from atlas.schema.record import (
    Capacity,
    FacilityRecord,
    FuzzyDate,
    Status,
    StatusEvent,
)

ACTIVE_ORDER: tuple[Status, ...] = (
    "announced",
    "proposed",
    "permitted",
    "under_construction",
    "operating",
)
STOPPED: tuple[Status, ...] = ("paused", "denied", "cancelled")
DERIVED_DATE_KEYS = (
    "first_reported",
    "announced",
    "application_filed",
    "approved",
    "construction_start",
    "operating_since",
    "cancelled",
)

StatusGroup = Literal["op", "uc", "pl", "cx", "pa"]
MwBasis = Literal["it", "facility", "utility_request"]

_GROUPS: dict[Status, StatusGroup] = {
    "operating": "op",
    "under_construction": "uc",
    "announced": "pl",
    "proposed": "pl",
    "permitted": "pl",
    "denied": "cx",
    "cancelled": "cx",
    "paused": "pa",
}


class RollupError(ValueError):
    """The record has no non-planned event, so it has no current status."""


def period_start(d: FuzzyDate) -> date:
    """The first day of the year, quarter or month, or the day itself."""
    year = int(d.value[:4])
    if d.precision == "year":
        return date(year, 1, 1)
    if d.precision == "quarter":
        quarter = int(d.value[-1])
        return date(year, 3 * (quarter - 1) + 1, 1)
    if d.precision == "month":
        return date(year, int(d.value[5:7]), 1)
    return date.fromisoformat(d.value)


def event_key(e: StatusEvent) -> tuple[date, int]:
    return (period_start(e.as_of), e.seq)


def _actual(record: FacilityRecord) -> list[StatusEvent]:
    return [e for e in record.status_history if not e.planned]


def latest_event(record: FacilityRecord) -> StatusEvent:
    """The latest non-planned event. Raises RollupError if every event is planned."""
    events = _actual(record)
    if not events:
        raise RollupError(f"{record.id}: every status event is planned")
    return max(events, key=event_key)


def _phase_deciding_events(record: FacilityRecord) -> dict[str, StatusEvent]:
    events = _actual(record)
    deciding: dict[str, StatusEvent] = {}
    for phase in record.phases:
        mine = [e for e in events if e.phase_id is None or e.phase_id == phase.phase_id]
        if mine:
            deciding[phase.phase_id] = max(mine, key=event_key)
    return deciding


def phase_statuses(record: FacilityRecord) -> dict[str, Status]:
    """Each phase's status: its latest non-planned event (phase_id equal or None).

    Phases without such an event are absent.
    """
    return {pid: e.status for pid, e in _phase_deciding_events(record).items()}


def rollup_status(record: FacilityRecord) -> Status:
    """The record's current status (07 §2.3)."""
    if not record.phases:
        return latest_event(record).status
    deciding = _phase_deciding_events(record)
    if not deciding:
        return latest_event(record).status
    active = [e.status for e in deciding.values() if e.status in ACTIVE_ORDER]
    if active:
        return max(active, key=ACTIVE_ORDER.index)
    return max(deciding.values(), key=event_key).status


def derive_dates(record: FacilityRecord) -> dict[str, FuzzyDate]:
    """The dates derived from non-planned events. Keys without a matching event are absent."""
    events = sorted(_actual(record), key=event_key)
    out: dict[str, FuzzyDate] = {}

    def first(key: str, match: list[StatusEvent]) -> None:
        if match:
            out[key] = match[0].as_of.model_copy()

    first("first_reported", events)
    first("announced", [e for e in events if e.event == "announced"])
    first("application_filed", [e for e in events if e.event == "application_filed"])
    first("approved", [e for e in events if e.event in ("approved", "permit_issued")])
    first("construction_start", [e for e in events if e.event == "construction_start"])
    first("operating_since", [e for e in events if e.status == "operating"])
    if events and rollup_status(record) == "cancelled":
        cancelled = [e for e in events if e.status == "cancelled"]
        if cancelled:
            out["cancelled"] = cancelled[-1].as_of.model_copy()
    return out


def apply_rollup(record: FacilityRecord) -> FacilityRecord:
    """A copy with status set to the rollup and the derived dates recomputed.

    Non-derived date keys already present (for example expected_in_service) are kept.
    """
    dates = {k: v for k, v in record.dates.items() if k not in DERIVED_DATE_KEYS}
    dates.update(derive_dates(record))
    return record.model_copy(update={"status": rollup_status(record), "dates": dates})


def status_group(s: Status) -> StatusGroup:
    """Map group: op, uc, pl (announced, proposed, permitted), cx (denied, cancelled), pa."""
    return _GROUPS[s]


def is_expanding(record: FacilityRecord) -> bool:
    """An operating record with a phase still in the pipeline (announced to under construction)."""
    if rollup_status(record) != "operating":
        return False
    return any(s in ACTIVE_ORDER[:4] for s in phase_statuses(record).values())


def mw_display(c: Capacity) -> tuple[float | None, MwBasis | None]:
    """it_mw, else facility_mw, else utility_request_mw, with the basis (07 §2.4)."""
    if c.it_mw is not None:
        return c.it_mw, "it"
    if c.facility_mw is not None:
        return c.facility_mw, "facility"
    if c.utility_request_mw is not None:
        return c.utility_request_mw, "utility_request"
    return None, None
