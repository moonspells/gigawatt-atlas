"""Rollup and pointer branches that review round 1 found untested (SV-5: R9, R10, P1, P6)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from atlas.jsonio import record_json
from atlas.schema.pointers import claim_pointers, resolve, unsupported_pointers
from atlas.schema.record import FacilityRecord
from atlas.schema.rollup import derive_dates, phase_statuses, rollup_status

MakeRecord = Callable[..., FacilityRecord]


def ev(seq: int, status: str, event: str, value: str, **kw: Any) -> dict[str, Any]:
    precision = {4: "year", 7: "month", 10: "day"}[len(value)]
    return {
        "seq": seq,
        "status": status,
        "event": event,
        "as_of": {"value": value, "precision": precision},
        "source_ids": ["s1"],
        **kw,
    }


def phases(*ids: str) -> list[dict[str, Any]]:
    return [{"phase_id": pid, "name": pid.upper(), "source_ids": ["s1"]} for pid in ids]


def test_r9_a_later_record_level_event_decides_every_phase(make_record: MakeRecord) -> None:
    r = make_record(
        phases=phases("p1", "p2"),
        status_history=[
            ev(1, "under_construction", "construction_start", "2025-01", phase_id="p1"),
            ev(2, "operating", "energized", "2026-03"),  # phase_id null: the whole campus
        ],
    )
    assert phase_statuses(r) == {"p1": "operating", "p2": "operating"}
    assert rollup_status(r) == "operating"


def test_r10_cancelled_date_is_the_latest_cancellation(make_record: MakeRecord) -> None:
    r = make_record(
        status_history=[
            ev(1, "cancelled", "withdrawn", "2025-01"),
            ev(2, "announced", "resumed", "2025-06"),
            ev(3, "cancelled", "cancelled", "2026-02"),
        ]
    )
    assert rollup_status(r) == "cancelled"
    assert derive_dates(r)["cancelled"].value == "2026-02"


def test_p1_geometry_ref_needs_no_source(make_record: MakeRecord) -> None:
    sources = record_json(make_record())["sources"]
    loc = record_json(make_record())["location"]
    assert isinstance(sources, list) and isinstance(sources[0], dict) and isinstance(loc, dict)
    fields = ("lat", "lon", "city", "county_name", "county_fips", "state_abbr")
    sources[0]["supports"] = ["/canonical_name", *(f"/location/{f}" for f in fields)]
    loc["geometry_ref"] = "osm:way/1"
    r = make_record(sources=sources, location=loc)
    assert "/location/geometry_ref" not in claim_pointers(r)
    assert unsupported_pointers(r) == []


def test_p6_a_list_index_has_no_leading_zero() -> None:
    doc = {"aliases": ["a", "b"]}
    assert resolve(doc, "/aliases/1") == (True, "b")
    assert resolve(doc, "/aliases/01") == (False, None)
    assert resolve(doc, "/aliases/0") == (True, "a")
    assert resolve(doc, "/aliases/00") == (False, None)
