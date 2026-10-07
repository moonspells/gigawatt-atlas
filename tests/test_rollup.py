from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from atlas.schema.record import Capacity, FacilityRecord, FuzzyDate
from atlas.schema.rollup import (
    ACTIVE_ORDER,
    STOPPED,
    RollupError,
    apply_rollup,
    derive_dates,
    is_expanding,
    latest_event,
    mw_display,
    period_start,
    phase_statuses,
    rollup_status,
    status_group,
)

MakeRecord = Callable[..., FacilityRecord]


def ev(seq: int, status: str, event: str, value: str, precision: str, **kw: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "status": status,
        "event": event,
        "as_of": {"value": value, "precision": precision},
        "source_ids": ["s1"],
        **kw,
    }


def phase(pid: str) -> dict[str, Any]:
    return {"phase_id": pid, "name": pid.upper(), "source_ids": ["s1"]}


@pytest.mark.parametrize(
    ("value", "precision", "start"),
    [
        ("2026", "year", date(2026, 1, 1)),
        ("2026-Q1", "quarter", date(2026, 1, 1)),
        ("2026-Q3", "quarter", date(2026, 7, 1)),
        ("2026-Q4", "quarter", date(2026, 10, 1)),
        ("2026-05", "month", date(2026, 5, 1)),
        ("2026-05-17", "day", date(2026, 5, 17)),
    ],
)
def test_period_start(value: str, precision: Any, start: date) -> None:
    assert period_start(FuzzyDate(value=value, precision=precision)) == start


def test_latest_event_orders_by_period_then_seq(make_record: MakeRecord) -> None:
    r = make_record(
        status_history=[
            ev(1, "announced", "announced", "2026-03", "month"),
            ev(2, "proposed", "application_filed", "2026-03-01", "day"),
            ev(3, "permitted", "approved", "2026", "year"),
        ]
    )
    # 2026 (year) starts 2026-01-01, so it is the earliest despite seq 3.
    assert latest_event(r).seq == 2
    assert r.status == "proposed"


def test_planned_events_are_ignored(make_record: MakeRecord) -> None:
    r = make_record(
        status_history=[
            ev(1, "proposed", "application_filed", "2026-01", "month"),
            ev(2, "under_construction", "construction_start", "2026-02", "month", planned=True),
        ]
    )
    assert r.status == "proposed"
    assert "construction_start" not in r.dates


def test_every_event_planned_raises(make_record: MakeRecord) -> None:
    r = make_record(
        rollup=False,
        status_history=[ev(1, "proposed", "hearing_scheduled", "2026-01", "month", planned=True)],
    )
    with pytest.raises(RollupError):
        rollup_status(r)
    with pytest.raises(RollupError):
        latest_event(r)


def test_phase_rollup_takes_the_most_advanced_active_phase(make_record: MakeRecord) -> None:
    r = make_record(
        phases=[phase("p1"), phase("p2")],
        status_history=[
            ev(1, "announced", "announced", "2024-02", "month"),
            ev(2, "operating", "energized", "2025-06", "month", phase_id="p1"),
            ev(3, "under_construction", "construction_start", "2026-03", "month", phase_id="p2"),
        ],
    )
    assert phase_statuses(r) == {"p1": "operating", "p2": "under_construction"}
    assert r.status == "operating"
    assert is_expanding(r)


def test_a_stopped_phase_does_not_stop_an_active_one(make_record: MakeRecord) -> None:
    r = make_record(
        phases=[phase("p1"), phase("p2")],
        status_history=[
            ev(1, "permitted", "approved", "2025-01", "month", phase_id="p1"),
            ev(2, "cancelled", "withdrawn", "2026-01", "month", phase_id="p2"),
        ],
    )
    assert r.status == "permitted"
    assert "cancelled" not in r.dates  # only when the record itself is cancelled


def test_stopped_only_takes_the_latest_stopped_phase(make_record: MakeRecord) -> None:
    r = make_record(
        phases=[phase("p1"), phase("p2")],
        status_history=[
            ev(1, "proposed", "application_filed", "2025-01", "month"),
            ev(2, "denied", "denied", "2025-06", "month", phase_id="p1"),
            ev(3, "paused", "paused", "2025-09", "month", phase_id="p2"),
        ],
    )
    assert phase_statuses(r) == {"p1": "denied", "p2": "paused"}
    assert r.status == "paused"


def test_phases_without_events_fall_back_to_the_latest_event(make_record: MakeRecord) -> None:
    r = make_record(
        phases=[phase("p1")],
        status_history=[ev(1, "proposed", "application_filed", "2025-01", "month", phase_id="p9")],
    )
    assert phase_statuses(r) == {}
    assert r.status == "proposed"


def test_derived_dates(make_record: MakeRecord) -> None:
    r = make_record(
        status_history=[
            ev(1, "announced", "announced", "2024-02", "month"),
            ev(2, "proposed", "application_filed", "2024-05-02", "day"),
            ev(3, "permitted", "permit_issued", "2024-09", "month"),
            ev(4, "under_construction", "construction_start", "2025-Q1", "quarter"),
            ev(5, "operating", "energized", "2026", "year"),
            ev(6, "operating", "phase_online", "2026-08", "month"),
        ],
        dates={"expected_in_service": {"value": "2027", "precision": "year"}},
    )
    got = {k: v.value for k, v in r.dates.items()}
    assert got == {
        "first_reported": "2024-02",
        "announced": "2024-02",
        "application_filed": "2024-05-02",
        "approved": "2024-09",
        "construction_start": "2025-Q1",
        "operating_since": "2026",
        "expected_in_service": "2027",
    }
    assert derive_dates(r) == {k: v for k, v in r.dates.items() if k != "expected_in_service"}


def test_cancelled_date_only_for_cancelled_records(make_record: MakeRecord) -> None:
    r = make_record(
        status_history=[
            ev(1, "proposed", "application_filed", "2025-11-04", "day"),
            ev(2, "cancelled", "withdrawn", "2026-05-12", "day"),
        ]
    )
    assert r.status == "cancelled"
    assert r.dates["cancelled"].value == "2026-05-12"


def test_apply_rollup_fixes_status_and_dates(make_record: MakeRecord) -> None:
    r = make_record(
        rollup=False,
        status="operating",
        dates={"first_reported": {"value": "2020", "precision": "year"}},
    )
    fixed = apply_rollup(r)
    assert fixed.status == "announced"
    assert fixed.dates["first_reported"].value == "2026-03"
    assert r.status == "operating"  # the input is not changed


def test_not_expanding_without_active_phases(make_record: MakeRecord) -> None:
    r = make_record(
        phases=[phase("p1")],
        status_history=[ev(1, "operating", "energized", "2025-01", "month")],
    )
    assert r.status == "operating"
    assert not is_expanding(r)


def test_status_group() -> None:
    assert [status_group(s) for s in ACTIVE_ORDER] == ["pl", "pl", "pl", "uc", "op"]
    assert [status_group(s) for s in STOPPED] == ["pa", "cx", "cx"]


def test_mw_display_prefers_it_then_facility_then_utility_request() -> None:
    assert mw_display(Capacity(it_mw=10, facility_mw=20, utility_request_mw=30)) == (10, "it")
    assert mw_display(Capacity(facility_mw=20, utility_request_mw=30)) == (20, "facility")
    assert mw_display(Capacity(utility_request_mw=30)) == (30, "utility_request")
    assert mw_display(Capacity(backup_generation_mw=5)) == (None, None)
