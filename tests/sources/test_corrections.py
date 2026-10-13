"""The shared correction mechanism (atlas/sources/corrections.py, docs/corrections.md): the Fix
model and its load checks, every record op with its guard, provenance, the derived fields, the
entry checks, and merge_import's corrections union, join, split and hold.

The cases of the design's prototype run are applied to the records the seed imports built for the
owner's corrections (tests/fixtures/corrections/seed-records.json, README.md there), and the ops
also to records built from the importers' own fixtures.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from atlas.geo.counties import CountyIndex
from atlas.geocode import Gazetteer
from atlas.jsonio import record_json
from atlas.net import FetchError
from atlas.schema.record import PLACEHOLDER_ID, Correction, FacilityRecord
from atlas.sources import aigridwatch, epoch
from atlas.sources.base import (
    Candidate,
    ImportContext,
    ImportReceipt,
    ImportResult,
    ReviewItem,
    merge_import,
)
from atlas.sources.corrections import (
    FIXES,
    Applied,
    Fix,
    apply_fixes,
    check_entries,
    check_fix_dates,
    correction_metrics,
    human_confidence,
    lost_events,
    mark_corrected,
    name_suffix,
    narrow_supports,
    place_location,
    remap_list_pointers,
)
from atlas.sources.osm import OsmImporter
from atlas.store import RecordStore
from atlas.validate import validate_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SEED = FIXTURES / "corrections" / "seed-records.json"
TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
FILE = "config/overrides/test.json"
REASON = "The reviewer read the cited page"

MakeContext = Callable[..., ImportContext]
Correct = Callable[..., Applied]


@functools.cache
def _seed_docs() -> dict[str, Any]:
    docs: dict[str, Any] = json.loads(SEED.read_text(encoding="utf-8"))
    return docs


def seed(key: str) -> FacilityRecord:
    """A copy of a seed record, by "{match key}:{value}"."""
    return FacilityRecord.model_validate(_seed_docs()[key])


def fix(
    url: str,
    quote: str,
    *ops: dict[str, Any],
    publisher: str = "Example News",
    source_type: str | None = "news",
    basis: str = "upstream_error",
    **extra: Any,
) -> dict[str, Any]:
    """One Fix as the overrides file writes it."""
    out: dict[str, Any] = {
        "source_url": url,
        "publisher": publisher,
        "quote": quote,
        "basis": basis,
        "reason": REASON,
        "retrieved_at": "2026-10-10T12:00:00Z",
        "reviewed_at": "2026-10-10",
        "ops": list(ops),
        **extra,
    }
    if source_type is not None:
        out["source_type"] = source_type
    return out


@pytest.fixture
def correct(counties: CountyIndex, sample_gazetteer: Gazetteer) -> Correct:
    """apply_fixes on the Fixes given (one entry, key "k" unless key= is passed)."""

    def apply(record: FacilityRecord, *fixes: dict[str, Any], key: str = "k", **kw: Any) -> Applied:
        parsed = FIXES.validate_python(list(fixes))
        return apply_fixes(
            record,
            [(key, parsed)],
            file=FILE,
            source="test",
            today=TODAY,
            counties=counties,
            gazetteer=sample_gazetteer,
            **kw,
        )

    return apply


def doc(record: FacilityRecord) -> dict[str, Any]:
    return dict(record_json(record))


def source(record: FacilityRecord, sid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(s for s in doc(record)["sources"] if s["id"] == sid)
    return found


def meta(record: FacilityRecord, pointer: str) -> dict[str, Any]:
    found: dict[str, Any] = doc(record)["field_meta"][pointer]
    return found


def events(record: FacilityRecord) -> list[tuple[str, str, str, str | None, list[str]]]:
    return [
        (e.event, e.status, e.as_of.value, e.phase_id, list(e.source_ids))
        for e in record.status_history
    ]


def changes(record: FacilityRecord) -> list[tuple[str, Any, Any]]:
    return [(c.pointer, c.old, c.new) for c in record.corrections]


def summary(event: str, status: str, as_of: str, **kw: Any) -> dict[str, Any]:
    return {
        "event": event,
        "status": status,
        "as_of": as_of,
        "phase_id": None,
        "planned": False,
    } | kw


def valid(record: FacilityRecord, counties: CountyIndex) -> bool:
    return validate_record(record, counties=counties, today=TODAY) == []


# ---------------------------------------------------------------------------- the Fix model


EVERY_OP: list[dict[str, Any]] = [
    {"op": "hold"},
    {"op": "hold", "kind": "unknown_status"},
    {"op": "out_of_scope"},
    {"op": "set", "path": "/location/postcode", "from": "68106", "value": "68102"},
    {"op": "set", "path": "/dates/expected_in_service", "from": None, "value": "2027-Q2"},
    {"op": "set", "phase": "p1", "path": "/capacity/it_mw", "from": None, "value": 70},
    {"op": "rename", "from": "Old", "base": "New", "old_as_alias": "osm_name"},
    {"op": "add_alias", "name": "Project Sabal", "kind": "codename"},
    {"op": "remove_alias", "name": "Old"},
    {"op": "set_party", "role": "operator", "from": ["A"], "names": ["B"]},
    {"op": "add_party", "role": "tenant", "name": "Amazon Web Services"},
    {"op": "remove_party", "role": "owner", "name": "A"},
    {"op": "rename_party", "role": "tenant", "from": "Cipher Mining", "to": "Cipher Digital"},
    {
        "op": "set_location",
        "from": {"precision": "locality"},
        "state_abbr": "IN",
        "county_fips": "18149",
        "precision": "county",
    },
    {"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2026-08"},
    {
        "op": "add_event",
        "event": "denied",
        "status": "denied",
        "as_of": "2026-08-25",
        "status_reason": "local_denial",
    },
    {"op": "replace_event", "match": {"event": "denied"}, "set": {"as_of": "2026-08-13"}},
    {"op": "remove_event", "match": {"event": "other", "status": "operating"}},
    {"op": "observe", "status": "operating", "as_of": "2026-07-28"},
    {"op": "first_report", "from": None, "as_of": "2022", "status": "announced"},
    {"op": "support", "event": {"event": "first_reported"}},
    {"op": "support", "path": "/location/county_fips", "from": "48029"},
]


def test_every_op_parses() -> None:
    (parsed,) = FIXES.validate_python([fix("https://example.org/a", "a quote", *EVERY_OP)])
    assert [op.op for op in parsed.ops] == [op["op"] for op in EVERY_OP]
    assert parsed.kind == "news"
    # Without source_type, the page's type comes from classify_source.
    (sec,) = FIXES.validate_python(
        [fix("https://www.sec.gov/x", "a quote", {"op": "hold"}, source_type=None)]
    )
    assert sec.kind == "sec_filing"


def with_op(op: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return fix("https://example.org/a", "a quote", op, **kw)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**with_op({"op": "hold"}), "colour": "red"}, "Extra inputs are not permitted"),
        (with_op({"op": "delete"}), "does not match any of the expected tags"),
        ({k: v for k, v in with_op({"op": "hold"}).items() if k != "quote"}, "Field required"),
        (fix("https://example.org/a", "", {"op": "hold"}), "at least 1 character"),
        (fix("https://example.org/a", "q" * 301, {"op": "hold"}), "at most 300 characters"),
        ({**with_op({"op": "hold"}), "reason": "r" * 301}, "at most 300 characters"),
        ({**with_op({"op": "hold"}), "basis": "typo"}, "'out_of_date', 'upstream_error'"),
        ({**with_op({"op": "hold"}), "ops": []}, "at least 1 item"),
        (
            fix("https://example.org/a", "call (571) 555-0100", {"op": "hold"}),
            "quote looks like an email address or phone number",
        ),
        (
            {**with_op({"op": "hold"}), "reason": "per jane@example.org"},
            "reason looks like an email address",
        ),
        ({**with_op({"op": "hold"}), "reason": "a\x07bell"}, "has control or invisible"),
        (
            with_op({"op": "rename", "from": "A", "base": "Mail ops@example.org"}),
            "ops[0] looks like an email address",
        ),
        (
            with_op({"op": "set", "path": "/location/city", "from": "A", "value": "B"}),
            "change through set_location",
        ),
        (
            with_op({"op": "set", "path": "/capacity/it_mw", "from": None, "value": "70 MW"}),
            "value must be a number or null",
        ),
        (
            with_op({"op": "set", "path": "/purpose", "from": "unknown", "value": "unknown"}),
            "value equals from",
        ),
        (
            with_op({"op": "set", "phase": "p", "path": "/site/acreage", "from": 1, "value": 2}),
            "is not a path of a phase",
        ),
        (
            with_op({"op": "rename", "from": "A", "base": "B (Ashburn, VA)"}),
            "ends with a place",
        ),
        (
            with_op(
                {
                    "op": "add_event",
                    "event": "approved",
                    "status": "permitted",
                    "as_of": "2026-08",
                    "status_reason": "local_denial",
                }
            ),
            "status_reason local_denial is for paused, denied or cancelled, not permitted",
        ),
        (
            with_op(
                {"op": "add_event", "event": "denied", "status": "denied", "as_of": "2026-08"},
                source_type="commercial_aggregator",
            ),
            "07 §6.4 gate 6",
        ),
        (
            with_op(
                {"op": "observe", "status": "paused", "as_of": "2026-08"},
                source_type="community_report",
            ),
            "07 §6.4 gate 6",
        ),
        ({**with_op({"op": "hold"}), "reviewed_at": "2026-10-09"}, "reviewed_at is before"),
        (with_op({"op": "remove_event", "match": {"as_of": "2026"}}), "names the event or"),
        (
            with_op({"op": "replace_event", "match": {"event": "denied"}, "set": {}}),
            "sets at least one",
        ),
        (
            with_op(
                {"op": "support", "path": "/purpose", "from": "x", "event": {"event": "other"}}
            ),
            "give path (with from) or event",
        ),
        (with_op({"op": "support", "path": "/status", "from": "operating"}), "needs no source"),
        (with_op({"op": "support", "path": "/purpose"}), "a path needs from"),
        (
            with_op(
                {
                    "op": "set_location",
                    "from": {"precision": "locality"},
                    "state_abbr": "IN",
                    "county_fips": "18149",
                    "precision": "county",
                    "street": "1 Main St",
                }
            ),
            "needs street precision or better",
        ),
        (
            with_op(
                {
                    "op": "set_location",
                    "from": {"town": "X"},
                    "state_abbr": "IN",
                    "precision": "state",
                }
            ),
            "from names fields a location does not have",
        ),
        (with_op({"op": "first_report", "as_of": "2022", "status": "announced"}), "Field required"),
        (with_op({"op": "observe", "status": "operating"}), "Field required"),
        (
            with_op({"op": "set_party", "role": "operator", "from": [], "names": ["A", "A Inc"]}),
            "names repeat an organization",
        ),
        (
            with_op({"op": "rename_party", "role": "tenant", "from": "A", "to": "A"}),
            "to equals from",
        ),
    ],
)
def test_a_bad_entry_does_not_load(entry: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        FIXES.validate_python([entry])


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"reviewed_at": "2026-10-13", "retrieved_at": "2026-10-12T00:00:00Z"}, "reviewed_at"),
        ({"retrieved_at": "2026-10-12T13:00:00Z", "reviewed_at": "2026-10-12"}, "retrieved_at"),
        ({"published_at": "2026-10-13T00:00:00Z"}, "published_at"),
        (
            {
                "ops": [
                    {
                        "op": "add_event",
                        "event": "energized",
                        "status": "operating",
                        "as_of": "2027",
                    }
                ]
            },
            "ops[0] as_of 2027",
        ),
        ({"ops": [{"op": "observe", "status": "operating", "as_of": "2026-11"}]}, "as_of 2026-11"),
    ],
)
def test_dates_after_today_stop_the_run(edit: dict[str, Any], message: str) -> None:
    (parsed,) = FIXES.validate_python([{**with_op({"op": "hold"}), **edit}])
    with pytest.raises(
        FetchError,
        match=r"entry 'k': corrections\[0\]: .*" + re.escape(message),
    ):
        check_fix_dates("k", [parsed], file=FILE, today=TODAY, now=NOW)


def test_a_planned_event_may_be_later() -> None:
    planned = {
        "op": "add_event",
        "event": "energized",
        "status": "operating",
        "as_of": "2027",
        "planned": True,
    }
    (parsed,) = FIXES.validate_python([with_op(planned)])
    check_fix_dates("k", [parsed], file=FILE, today=TODAY, now=NOW)


# ---------------------------------------------------------------------------- confidence


@pytest.mark.parametrize(
    ("kind", "replaced", "expected"),
    [
        ("news", [], 0.95),
        ("trade_press", ["open_dataset"], 0.95),
        ("company_release", ["open_dataset"], 0.99),  # 1.00, clamped
        ("government_record", ["news"], 0.99),
        ("sec_filing", [], 0.99),  # 1.02, clamped
        ("utility_or_iso_filing", ["court_or_regulator"], 0.99),  # one rank: no conflict term
        ("commercial_aggregator", [], 0.85),
        # 07 §3.4 literally: an open dataset ranks above a commercial aggregator, so Baxtel's
        # correction of an OSM value is 0.95 - 0.10 - 0.20 (an owner question, not decided here).
        ("commercial_aggregator", ["open_dataset"], 0.65),
        ("news", ["government_record"], 0.75),
        ("community_report", ["government_record"], 0.55),
    ],
)
def test_human_confidence(kind: str, replaced: list[str], expected: float) -> None:
    assert human_confidence(kind, replaced) == expected


# ---------------------------------------------------------------------------- pointers


def pointer_doc() -> dict[str, Any]:
    return {
        "aliases": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        "parties": {"operator": [{"name": "X"}], "tenant": [{"name": "T0"}, {"name": "T1"}]},
        "location": {"lat": 1.0, "lon": 2.0, "street": None, "precision": "site"},
        "sources": [
            {"id": "s1", "supports": ["/parties", "/aliases/2", "/location"]},
            {"id": "s2", "supports": ["/parties/tenant/0", "/aliases"]},
            {"id": "s3", "supports": ["/location/lat"]},
        ],
        "field_meta": {"/aliases/1": {"x": 1}, "/aliases/2": {"x": 2}, "/aliases": {"x": 0}},
    }


def test_narrow_supports() -> None:
    doc = pointer_doc()
    # "/parties" becomes its other values: the operator and the other tenant.
    assert narrow_supports(doc, "/parties/tenant/0", keep="s3") == ["s1", "s2"]
    assert doc["sources"][0]["supports"] == [
        "/parties/operator",
        "/parties/tenant/1",
        "/aliases/2",
        "/location",
    ]
    assert doc["sources"][1]["supports"] == ["/aliases"]  # the entry equal to it is dropped
    # A null value and an exempt one (/location/precision) need no support.
    assert narrow_supports(doc, "/location/lat") == ["s1", "s3"]
    assert doc["sources"][0]["supports"][-1] == "/location/lon"
    assert doc["sources"][2]["supports"] == []
    # An entry inside the corrected value is dropped: its source no longer states the value.
    doc = pointer_doc()
    assert narrow_supports(doc, "/location", keep="s1") == ["s3"]
    assert doc["sources"][2]["supports"] == []


def test_narrow_supports_keeps_only_the_items_a_source_cites() -> None:
    # Items that cite their own sources (events, aliases, organizations): a source's
    # "/status_history" becomes the other events that cite it, never the ones citing others only.
    # Epoch's first report (s4) dates event 2; Epoch's own rows (s1) are events 0 and 1.
    def doc() -> dict[str, Any]:
        return {
            "status_history": [
                {"event": "construction_start", "source_ids": ["s1"]},
                {"event": "energized", "source_ids": ["s1"]},
                {"event": "first_reported", "source_ids": ["s4"]},
            ],
            "sources": [
                {"id": "s1", "supports": ["/status_history"]},
                {"id": "s4", "supports": ["/status_history"]},
            ],
        }

    redated = doc()  # a correction redates the first report: s4 claims no other event
    assert narrow_supports(redated, "/status_history/2", keep="s9") == ["s1", "s4"]
    assert redated["sources"][0]["supports"] == ["/status_history/0", "/status_history/1"]
    assert redated["sources"][1]["supports"] == []
    added = doc()  # a correction's event at index 0: s4 keeps its own event
    assert narrow_supports(added, "/status_history/0", keep="s9") == ["s1", "s4"]
    assert added["sources"][0]["supports"] == ["/status_history/1"]
    assert added["sources"][1]["supports"] == ["/status_history/2"]


def test_remap_list_pointers() -> None:
    doc = pointer_doc()
    remap_list_pointers(doc, "/aliases", 1)
    assert doc["sources"][0]["supports"] == ["/parties", "/aliases/1", "/location"]
    assert doc["sources"][1]["supports"] == ["/parties/tenant/0", "/aliases"]
    assert doc["field_meta"] == {"/aliases/1": {"x": 2}, "/aliases": {"x": 0}}


# ---------------------------------------------------------------------------- the prototype's cases


def test_set_a_postcode(correct: Correct, counties: CountyIndex) -> None:
    """1623 Farnam's addr:postcode 68106 is wrong; the building's own site gives 68102."""
    before = seed("osm:way/818167571")
    rec = correct(
        before,
        fix(
            "https://www.1623farnam.com/",
            "1623 Farnam Street, Suite 860 Omaha, NE 68102",
            {"op": "set", "path": "/location/postcode", "from": "68106", "value": "68102"},
            publisher="1623 Farnam",
            source_type="company_release",
            issue_url="https://github.com/moonman36/gigawatt-atlas/pull/2",
        ),
    ).record
    assert rec.location.postcode == "68102"
    assert rec.canonical_name == before.canonical_name
    s3 = source(rec, "s3")
    assert (s3["url"], s3["quote_match"], s3["supports"], s3["source_type"]) == (
        "https://www.1623farnam.com/",
        "human",
        ["/location/postcode"],
        "company_release",
    )
    assert "/location/postcode" in before.sources[0].supports
    assert "/location/postcode" not in source(rec, "s1")["supports"]
    assert source(rec, "s1")["supports"] == [
        p for p in before.sources[0].supports if p != "/location/postcode"
    ]
    assert meta(rec, "/location/postcode") == {
        "confidence": 0.99,
        "method": "human_entered",
        "source_ids": ["s3"],
        "conflicts": [
            {"value": "68106", "source_ids": ["s1"], "note": "replaced by a cited correction"}
        ],
    }
    assert rec.corrections == [
        Correction(
            on=date(2026, 10, 10),
            pointer="/location/postcode",
            old="68106",
            new="68102",
            reason=REASON,
            issue_url="https://github.com/moonman36/gigawatt-atlas/pull/2",  # type: ignore[arg-type]
        )
    ]
    assert rec.review.state == "machine"
    assert valid(rec, counties)


def test_remove_an_observation_and_add_an_approval(correct: Correct) -> None:
    """Wichita Falls: OSM's telecom=data_center reads operating; the city approved a
    conditional use for the existing building in August 2026."""
    rec = correct(
        seed("osm:way/747985149"),
        fix(
            "https://www.newschannel6now.com/2026/08/13/datanovax-data-center-approved/",
            "the Planning and Zoning Commission approved a conditional-use request",
            {"op": "remove_event", "match": {"event": "other", "status": "operating"}},
            {"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2026-08"},
        ),
    ).record
    assert rec.status == "permitted"
    assert events(rec) == [("approved", "permitted", "2026-08", None, ["s3"])]
    assert {k: v.value for k, v in rec.dates.items()} == {
        "first_reported": "2026-08",
        "approved": "2026-08",
    }
    # The status no longer claims OSM's 0.60 "imported": a reviewer's event decides it.
    assert meta(rec, "/status") == {
        "confidence": 0.95,
        "method": "human_entered",
        "source_ids": ["s3"],
        "conflicts": [],
    }
    (lost,) = meta(rec, "/status_history")["conflicts"]
    assert lost["value"] == summary("other", "operating", "2026-10-09")
    assert lost["source_ids"] == ["s1"]
    assert changes(rec) == [
        ("/status_history", summary("other", "operating", "2026-10-09"), None),
        ("/status_history", None, summary("approved", "permitted", "2026-08")),
    ]


def test_add_an_event_narrows_the_row_source(correct: Correct) -> None:
    """Burkhalter Road: Statesboro's council approved the rezoning on 2026-08-18. AI
    GridWatch's s1 supported all of /status_history; it now supports its own two events."""
    rec = correct(
        seed("aigridwatch_id:burkhalter-road-statesboro-ga"),
        fix(
            "https://www.wtoc.com/2026/08/19/statesboro-city-council-approves-rezoning/",
            "Statesboro City Council voted to approve a zoning map amendment",
            {"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2026-08-18"},
        ),
    ).record
    assert (rec.status, rec.dates["approved"].value) == ("permitted", "2026-08-18")
    assert events(rec)[-1] == ("approved", "permitted", "2026-08-18", None, ["s3"])
    supports = source(rec, "s1")["supports"]
    assert "/status_history" not in supports
    assert supports[-2:] == ["/status_history/0", "/status_history/1"]
    assert rec.status_history[-1].seq == 3


def test_a_denial_and_an_earlier_first_report(correct: Correct) -> None:
    """Aligned Pataskala: the council rejected the site plan on 2026-08-25 (local denial); a
    2023 article already reported Aligned targeting Pataskala."""
    before = seed("aigridwatch_id:aligned-pataskala-oh")
    assert before.dates["first_reported"].value == "2025-11"
    rec = correct(
        before,
        fix(
            "https://www.thereportingproject.org/pataskala-residents-cheer/",
            "the city council voted unanimously to reject a proposal",
            {
                "op": "add_event",
                "event": "denied",
                "status": "denied",
                "as_of": "2026-08-25",
                "status_reason": "local_denial",
            },
        ),
        fix(
            "https://digital-dev.nbc4i.com/news/aligned-pataskala/amp/",
            "Aligned Data Centers appears to be targeting Pataskala",
            {"op": "first_report", "from": "2025-11", "as_of": "2023-11-02", "status": "announced"},
            published_at="2023-11-02T00:00:00Z",
        ),
    ).record
    assert (rec.status, rec.status_reason) == ("denied", "local_denial")
    assert rec.dates["first_reported"].value == "2023-11-02"
    assert events(rec) == [
        ("other", "proposed", "2025-11", None, ["s1"]),
        ("denied", "denied", "2026-08-25", None, ["s3"]),
        ("first_reported", "announced", "2023-11-02", None, ["s4"]),
    ]
    assert source(rec, "s4")["published_at"] == "2023-11-02T00:00:00Z"
    assert changes(rec)[-1] == (
        "/status_history",
        [summary("first_reported", "proposed", "2025-11")],
        summary("first_reported", "announced", "2023-11-02"),
    )


def test_redate_an_event(correct: Correct) -> None:
    """King of Prussia: the supervisors denied the five sites on August 13, not 14."""
    rec = correct(
        seed("aigridwatch_id:king-of-prussia-mlp-pa"),
        fix(
            "https://www.fox29.com/news/upper-merion-board-supervisors-denies",
            "denied all five proposed data center sites by MLP Ventures on Thursday, August 13.",
            {
                "op": "replace_event",
                "match": {"event": "denied", "as_of": "2026-08-14"},
                "set": {"as_of": "2026-08-13"},
            },
        ),
    ).record
    assert events(rec)[1] == ("denied", "denied", "2026-08-13", None, ["s3"])
    # The importer's status_reason stays: the status is still denied.
    assert (rec.status, rec.status_reason) == ("denied", "local_denial")
    (lost,) = meta(rec, "/status_history")["conflicts"]
    assert (lost["value"]["as_of"], lost["source_ids"]) == ("2026-08-14", ["s1"])
    assert meta(rec, "/status")["source_ids"] == ["s3"]
    assert source(rec, "s1")["supports"][-1] == "/status_history/0"


def test_retype_an_event(correct: Correct) -> None:
    """Amazon Bridgefield: the 2026-03 date is the first report of an earlier purchase, not
    Amazon's announcement; a retype keeps AI GridWatch's source and adds the reviewer's."""
    rec = correct(
        seed("aigridwatch_id:amazon-bridgefield-ashburn-va"),
        fix(
            "https://wtop.com/loudoun-county/2026/08/concrete-jungle/",
            "about five months after Amazon Data Services bought the GW campus",
            {
                "op": "replace_event",
                "match": {"event": "announced", "as_of": "2026-03-17"},
                "set": {"event": "first_reported"},
            },
        ),
    ).record
    assert events(rec)[0] == ("first_reported", "announced", "2026-03-17", None, ["s1", "s3"])
    assert "announced" not in rec.dates
    assert rec.dates["first_reported"].value == "2026-03-17"
    assert "/status_history" not in rec.field_meta  # nothing was lost


def test_set_location_at_a_county(correct: Correct, counties: CountyIndex) -> None:
    """Abei Energy: the rezoning is of two parcels in Starke County; AI GridWatch's point is a
    guess, so the record is placed at the county."""
    rec = correct(
        seed("aigridwatch_id:abei-energy-data-center-starke-in"),
        fix(
            "https://wkvi.com/2025/11/starke-county-commissioners/",
            "rezoning two parcels of land from Agricultural to Industrial in Starke County",
            {
                "op": "set_location",
                "from": {"precision": "locality"},
                "state_abbr": "IN",
                "county_fips": "18149",
                "precision": "county",
            },
        ),
    ).record
    loc = rec.location
    assert (loc.precision, loc.county_fips, loc.city, loc.geocode_method) == (
        "county",
        "18149",
        None,
        "county_centroid",
    )
    assert (loc.lat, loc.lon) == counties.centroid("18149")
    assert rec.canonical_name.endswith(" (Starke County, IN)")
    assert "/location" not in source(rec, "s1")["supports"]
    assert source(rec, "s3")["supports"] == ["/location"]
    fm = meta(rec, "/location")
    assert (fm["method"], fm["source_ids"]) == ("human_entered", ["s3"])
    assert fm["conflicts"][0]["value"]["precision"] == "locality"
    assert fm["conflicts"][0]["source_ids"] == ["s1"]


def test_set_location_renames_and_events_follow(correct: Correct, counties: CountyIndex) -> None:
    """Microsoft Cheyenne: AI GridWatch's Operating is Microsoft's April 2026 announcement, the
    council approved the annexation on 2026-07-13, and the site is in Laramie County: the place
    in the name follows the location, with no rename op."""
    before = seed("aigridwatch_id:microsoft-cheyenne-expansion")
    assert before.canonical_name == "Microsoft Cheyenne Expansion (WY)"
    rec = correct(
        before,
        fix(
            "https://news.microsoft.com/source/2026/04/14/microsoft-cheyenne/",
            "announced its intent to purchase approximately 3,200 acres of land",
            {"op": "remove_event", "match": {"event": "other", "status": "operating"}},
            {"op": "add_event", "event": "announced", "status": "announced", "as_of": "2026-04-14"},
            publisher="Microsoft",
            source_type="company_release",
        ),
        fix(
            "https://county17.com/2026/07/14/cheyenne-passes-annexation/",
            "the council approved the annexation on a 6-3 vote",
            {"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2026-07-13"},
            {
                "op": "set_location",
                "from": {"precision": "locality", "county_fips": None},
                "state_abbr": "WY",
                "county_fips": "56021",
                "precision": "county",
            },
        ),
    ).record
    assert rec.canonical_name == "Microsoft Cheyenne Expansion (Laramie County, WY)"
    assert rec.status == "permitted"
    assert {k: v.value for k, v in rec.dates.items()} == {
        "first_reported": "2026-04-14",
        "announced": "2026-04-14",
        "approved": "2026-07-13",
    }
    assert meta(rec, "/status") == {
        "confidence": 0.95,
        "method": "human_entered",
        "source_ids": ["s4"],
        "conflicts": [],
    }
    assert valid(rec, counties)


def test_redate_reuses_a_cited_source(correct: Correct) -> None:
    """Microsoft-Nebius: SemiAnalysis, which the record already cites (s7, no quote), dates the
    first 25 MW to about October 2025. Its source is reused and takes the quote; a second quote of
    the same page is another source."""
    before = seed("epoch_name:Microsoft-Nebius New Jersey")
    url = "https://newsletter.semianalysis.com/p/stop-saying-half-of-2026-us-datacenter"
    assert source(before, "s7")["url"] == url and source(before, "s7")["quote"] is None
    quote = "~October 2025: first 25 MW delivered, seen by the installed chillers."
    redate = {
        "op": "replace_event",
        "match": {"event": "energized", "as_of": "2026-Q1"},
        "set": {"as_of": "2025-Q4"},
    }
    rec = correct(
        before,
        fix(url, quote, redate, publisher="SemiAnalysis", source_type="trade_press"),
        fix(
            url,
            "first 25 MW delivered",
            {"op": "support", "event": {"event": "energized"}},
            publisher="SemiAnalysis",
            source_type="trade_press",
        ),
    ).record
    s7 = source(rec, "s7")
    assert (s7["quote"], s7["quote_match"], s7["publisher"], s7["retrieved_at"]) == (
        quote,
        "human",
        "SemiAnalysis",
        "2026-10-10T12:00:00Z",
    )
    assert [s.id for s in rec.sources][-2:] == ["s7", "s8"]
    assert events(rec)[1] == ("energized", "operating", "2025-Q4", None, ["s7", "s8"])
    assert rec.dates["operating_since"].value == "2025-Q4"


def test_rename_a_tenant_narrows_the_dataset_source(correct: Correct) -> None:
    """Anthropic Barber Lake: Cipher Mining is now Cipher Digital (SEC). Epoch's s1 supported
    /parties; it keeps the tenant it still states."""
    rec = correct(
        seed("epoch_name:Anthropic Barber Lake"),
        fix(
            "https://data.sec.gov/submissions/CIK0001819989.json",
            '"name":"Cipher Digital Inc."',
            {
                "op": "rename_party",
                "role": "tenant",
                "from": "Cipher Mining",
                "to": "Cipher Digital",
            },
            publisher="U.S. Securities and Exchange Commission",
            source_type="sec_filing",
        ),
    ).record
    tenant = rec.parties.tenant[0]
    assert (tenant.name, tenant.source_ids) == ("Cipher Digital", ["s1", "s8"])
    assert "/parties/tenant/1" in source(rec, "s1")["supports"]
    assert "/parties" not in source(rec, "s1")["supports"]
    assert changes(rec) == [("/parties/tenant", "Cipher Mining", "Cipher Digital")]


def test_record_type_hook(correct: Correct) -> None:
    """Amazon Ridgeland is in the early stages of construction: no building operates, so Epoch's
    record_type rule makes it a project again, and Epoch's modelled IT power goes."""

    def epoch_type(r: FacilityRecord) -> Any:
        operating = any(e.status == "operating" and not e.planned for e in r.status_history)
        return "campus" if operating else "project"

    before = seed("epoch_name:Amazon Ridgeland")
    assert before.record_type == "campus"
    rec = correct(
        before,
        fix(
            "https://ourtupelo.com/where-do-amazons-data-centers-in-mississippi-stand/",
            "The Ridgeland site is in the early stages of construction",
            {"op": "remove_event", "match": {"event": "energized", "as_of": "2026-05-15"}},
            {"op": "set", "path": "/capacity/it_mw", "from": 228.0, "value": None},
        ),
        record_type=epoch_type,
    ).record
    assert (rec.record_type, rec.status, rec.capacity.it_mw) == (
        "project",
        "under_construction",
        None,
    )
    assert "operating_since" not in rec.dates
    assert source(rec, "s1")["supports"][3] == "/capacity/facility_mw"
    fm = meta(rec, "/capacity/it_mw")
    assert fm["conflicts"] == [
        {"value": 228.0, "source_ids": ["s1"], "note": "replaced by a cited correction"}
    ]
    assert changes(rec)[-1] == ("/capacity/it_mw", 228.0, None)


def test_support_an_event_and_add_an_alias(correct: Correct) -> None:
    """Meta Aiken: the Aiken Standard ties the first report to the site and names its codename,
    Project Sabal."""
    rec = correct(
        seed("epoch_name:Meta Aiken"),
        fix(
            "https://www.postandcourier.com/aikenstandard/business/meta-aiken.html",
            "originally referred to the data center owner as Project Sabal",
            {"op": "support", "event": {"event": "first_reported"}},
            {"op": "add_alias", "name": "Project Sabal", "kind": "codename"},
            publisher="Aiken Standard",
        ),
    ).record
    assert events(rec)[0][-1] == ["s4", "s5"]
    assert [(a.name, a.kind, a.source_ids) for a in rec.aliases] == [
        ("Project Sabal", "codename", ["s5"])
    ]
    assert [c.pointer for c in rec.corrections] == ["/status_history", "/aliases"]


def test_rename_keeps_the_old_name_as_an_alias(correct: Correct) -> None:
    """Aligned IAD-02 is one campus with IAD-01 (Baxtel). The name OSM gave becomes an osm_name
    alias citing OSM; the commercial aggregator's correction of an open dataset's value is 0.65
    (07 §3.4 as written)."""
    rec = correct(
        seed("osm:way/793622740"),
        fix(
            "https://baxtel.com/campus/aligned-ashburn-campus",
            "2 data centers on this campus ... Aligned: IAD-01 ... Aligned: IAD-02",
            {
                "op": "rename",
                "from": "Aligned IAD-02",
                "base": "Aligned IAD-01 and IAD-02",
                "old_as_alias": "osm_name",
            },
            publisher="Baxtel",
            source_type="commercial_aggregator",
        ),
    ).record
    assert rec.canonical_name == "Aligned IAD-01 and IAD-02 (Ashburn, VA)"
    assert (rec.aliases[-1].name, rec.aliases[-1].kind, rec.aliases[-1].source_ids) == (
        "Aligned IAD-02",
        "osm_name",
        ["s1"],
    )
    assert "/canonical_name" not in source(rec, "s1")["supports"]
    assert source(rec, "s5")["supports"] == ["/canonical_name"]
    fm = meta(rec, "/canonical_name")
    assert (fm["confidence"], fm["conflicts"][0]["value"]) == (0.65, "Aligned IAD-02 (Ashburn, VA)")
    assert changes(rec) == [
        (
            "/canonical_name",
            "Aligned IAD-02 (Ashburn, VA)",
            "Aligned IAD-01 and IAD-02 (Ashburn, VA)",
        )
    ]


def test_two_fixes_from_two_sources(correct: Correct) -> None:
    """Crown Castle Baltimore: Crown Castle sold its fiber business to Zayo (operator, cited to
    Crown Castle), and PeeringDB names the site (the name, cited to PeeringDB)."""
    rec = correct(
        seed("osm:node/9669340912"),
        fix(
            "https://www.crowncastle.com/directory-page",
            "We've sold our Fiber Solutions business to Zayo",
            {
                "op": "set_party",
                "role": "operator",
                "from": ["Crown Castle Fiber"],
                "names": ["Zayo"],
            },
            publisher="Crown Castle",
            source_type="company_release",
        ),
        fix(
            "https://www.peeringdb.com/api/fac?city=Baltimore&state=MD",
            "Zayo Baltimore - 111 Market Pl",
            {
                "op": "rename",
                "from": "Crown Castle Baltimore",
                "base": "Zayo Baltimore - 111 Market Place",
            },
            publisher="PeeringDB",
            source_type="commercial_aggregator",
        ),
    ).record
    assert rec.canonical_name == "Zayo Baltimore - 111 Market Place (Baltimore, MD)"
    (operator,) = rec.parties.operator
    assert (operator.name, operator.source_ids) == ("Zayo", ["s3"])
    fm = meta(rec, "/parties/operator")
    assert (fm["confidence"], fm["source_ids"], fm["conflicts"][0]["value"]) == (
        0.99,
        ["s3"],
        ["Crown Castle Fiber"],
    )
    assert fm["conflicts"][0]["source_ids"] == ["s1"]
    assert meta(rec, "/canonical_name")["source_ids"] == ["s4"]


def test_out_of_scope_on_a_record_already_out_of_scope(correct: Correct) -> None:
    """A process server's office: the importer's scope screen already holds it out of scope; the
    reviewer's decision changes no scope and still files its item."""
    before = seed("osm:node/7578821329")
    assert before.scope == "out_of_scope"
    applied = correct(
        before,
        fix(
            "https://www.serve-now.com/moreinfo/all-about-serving-process-server/10638",
            "Process Servers, providing process server service in Mesa",
            {"op": "out_of_scope"},
            publisher="Serve-Now",
            source_type="commercial_aggregator",
        ),
        key="node/7578821329",
        external_id="node/7578821329",
    )
    assert applied.record.scope == "out_of_scope" and applied.record.corrections == []
    (item,) = applied.review
    assert (item.source, item.kind, item.external_id, item.record_id) == (
        "test",
        "out_of_scope",
        "node/7578821329",
        None,
    )
    assert item.reason.startswith(
        f"out of scope by {FILE} entry 'node/7578821329' (reviewed 2026-10-10): {REASON} "
        "(https://www.serve-now.com/"
    )
    assert item.data == {
        "source_url": "https://www.serve-now.com/moreinfo/all-about-serving-process-server/10638",
        "quote": "Process Servers, providing process server service in Mesa",
        "reviewed_at": "2026-10-10",
        "basis": "upstream_error",
        "file": FILE,
    }


def test_phase_ops_and_an_observation(correct: Correct) -> None:
    """CoreWeave Muskogee: Core Scientific reports the 70 MW building billing, so it operates;
    Epoch's planned operation goes, and the phase's capacity is the page's own words."""

    def epoch_type(r: FacilityRecord) -> Any:
        operating = any(e.status == "operating" and not e.planned for e in r.status_history)
        return "campus" if operating else "project"

    phase = "coreweave-muskogee-ok"
    rec = correct(
        seed("epoch_name:CoreWeave Muskogee OK"),
        fix(
            "https://investors.corescientific.com/news-events/2q26-prepared-remarks",
            "We achieved that milestone ahead of schedule, reaching 437 billable megawatts",
            {"op": "remove_event", "match": {"event": "energized", "planned": True}},
            {"op": "observe", "status": "operating", "phase": phase, "as_of": "2026-07-28"},
            publisher="Core Scientific",
            source_type="company_release",
        ),
        fix(
            "https://investors.corescientific.com/news-events/press-releases/detail/135",
            "the Company's current leased 70 MW building designed to support the Nvidia GB300",
            {
                "op": "set",
                "phase": phase,
                "path": "/capacity/mw_as_stated",
                "from": None,
                "value": "70 MW building",
            },
            publisher="Core Scientific",
            source_type="company_release",
        ),
        record_type=epoch_type,
    ).record
    assert (rec.status, rec.record_type) == ("operating", "campus")
    assert rec.phases[0].capacity.mw_as_stated == "70 MW building"
    assert events(rec)[-1] == ("other", "operating", "2026-07-28", phase, ["s5"])
    assert rec.dates == {}  # an observation dates nothing
    assert source(rec, "s6")["supports"] == ["/phases/0/capacity/mw_as_stated"]
    assert meta(rec, "/status")["confidence"] == 0.99
    assert changes(rec)[-1] == (f"/phases/{phase}/capacity/mw_as_stated", None, "70 MW building")


def test_remove_every_operator(correct: Correct) -> None:
    """Stream DFW IV: Stream sold the building in 2011, and the buyer is not stated."""
    rec = correct(
        seed("osm:way/597835788"),
        fix(
            "https://www.streamdatacenters.com/locations/dallas-data-centers/",
            "We leased the facility to a major healthcare provider in early 2011 and sold it",
            {"op": "set_party", "role": "operator", "from": ["Stream"], "names": []},
            publisher="Stream Data Centers",
            source_type="company_release",
        ),
    ).record
    assert rec.parties.operator == []
    fm = meta(rec, "/parties/operator")
    assert (fm["source_ids"], fm["conflicts"][0]["value"]) == (["s3"], ["Stream"])


def test_hold_unpublishes(correct: Correct) -> None:
    """Sarasota County voted to halt every major data center for a year: the row is held."""
    applied = correct(
        seed("aigridwatch_id:sarasota-county-moratorium-fl"),
        fix(
            "https://www.wusf.org/politics-issues/2026-07-10/sarasota-county-blocks",
            "Commissioners voted on Wednesday to halt the acceptance, review and approval",
            {"op": "hold", "kind": "conflict"},
        ),
        key="sarasota-county-moratorium-fl",
    )
    assert applied.record.scope == "out_of_scope"
    assert changes(applied.record) == [("/scope", "in_scope", "out_of_scope")]
    (item,) = applied.review
    assert (item.kind, item.external_id) == ("conflict", "sarasota-county-moratorium-fl")
    assert item.reason.startswith(f"held by {FILE} entry 'sarasota-county-moratorium-fl'")


def test_redate_a_first_report(correct: Correct) -> None:
    """Posey County: the residents' meeting was on Thursday, 2026-06-04, not 06-03."""
    rec = correct(
        seed("aigridwatch_id:posey-county-boberg-hoenert-in"),
        fix(
            "https://www.14news.com/2026/06/05/posey-co-residents-pack-meeting/",
            "Close to 100 Posey County residents filled the Alexandrian Public Library Thursday",
            {
                "op": "replace_event",
                "match": {"event": "first_reported", "as_of": "2026-06-03"},
                "set": {"as_of": "2026-06-04"},
            },
        ),
    ).record
    assert rec.dates["first_reported"].value == "2026-06-04"
    assert events(rec) == [("first_reported", "announced", "2026-06-04", None, ["s3"])]


# ---------------------------------------------------------------------------- stale guards


@pytest.mark.parametrize(
    ("key", "op", "message"),
    [
        (
            "osm:way/818167571",
            {"op": "set", "path": "/location/postcode", "from": "68199", "value": "68102"},
            "/location/postcode is '68106', not '68199' (stale",
        ),
        (
            "aigridwatch_id:king-of-prussia-mlp-pa",
            {
                "op": "replace_event",
                "match": {"event": "denied", "as_of": "2026-08-15"},
                "set": {"as_of": "2026-08-13"},
            },
            "0 events match event='denied', as_of='2026-08-15', not 1 (stale",
        ),
        (
            "aigridwatch_id:posey-county-boberg-hoenert-in",
            {
                "op": "first_report",
                "from": "2026-06-03",
                "as_of": "2026-06-04",
                "status": "announced",
            },
            "first report 2026-06-04 is not earlier than the record's first_reported on 2026-06-03",
        ),
        (
            "osm:way/793622740",
            {"op": "rename", "from": "Aligned IAD-2", "base": "Aligned IAD-01 and IAD-02"},
            "the name is 'Aligned IAD-02', not 'Aligned IAD-2' (stale",
        ),
        (
            "aigridwatch_id:posey-county-boberg-hoenert-in",
            {"op": "first_report", "from": None, "as_of": "2025", "status": "announced"},
            "dates.first_reported is '2026-06-03', not None (stale",
        ),
        (
            "epoch_name:Meta Aiken",
            {
                "op": "add_event",
                "event": "construction_start",
                "status": "under_construction",
                "as_of": "2024-05-30",
            },
            "the record has construction_start on 2024-05-30 already (stale",
        ),
        (
            "epoch_name:Meta Aiken",
            {"op": "observe", "status": "operating", "as_of": "2026-08-07", "phase": "x"},
            "the record has no phase 'x'",
        ),
        (
            "osm:way/793622740",
            {"op": "add_alias", "name": "aligned iad-01", "kind": "osm_name"},
            "the record has the alias 'aligned iad-01' (stale",
        ),
        (
            "osm:way/793622740",
            {"op": "add_alias", "name": "Aligned IAD 02", "kind": "osm_name"},
            "alias 'Aligned IAD 02' is the record's name",
        ),
        (
            "osm:way/793622740",
            {"op": "remove_alias", "name": "Aligned IAD-03"},
            "the record has no alias 'Aligned IAD-03' (stale",
        ),
        (
            "osm:way/597835788",
            {"op": "set_party", "role": "operator", "from": ["Stream Data Centers"], "names": []},
            "operator is ['Stream'], not ['Stream Data Centers'] (stale",
        ),
        (
            "osm:way/597835788",
            {"op": "add_party", "role": "operator", "name": "Stream, Inc."},
            "operator 'Stream, Inc.' is in the record (stale",
        ),
        (
            "osm:way/597835788",
            {"op": "remove_party", "role": "owner", "name": "Stream"},
            "the record has no owner 'Stream' (stale",
        ),
        (
            "epoch_name:Anthropic Barber Lake",
            {"op": "rename_party", "role": "tenant", "from": "Cipher Mining", "to": "Anthropic"},
            "tenant 'Anthropic' is in the record already",
        ),
        (
            "aigridwatch_id:abei-energy-data-center-starke-in",
            {
                "op": "set_location",
                "from": {"precision": "county"},
                "state_abbr": "IN",
                "county_fips": "18149",
                "precision": "county",
            },
            "location.precision is 'locality', not 'county' (stale",
        ),
        (
            "aigridwatch_id:abei-energy-data-center-starke-in",
            {
                "op": "set_location",
                "from": {"precision": "locality"},
                "state_abbr": "IN",
                "county_fips": "48029",
                "precision": "county",
            },
            "county_fips 48029 is not a county of IN",
        ),
        (
            "aigridwatch_id:abei-energy-data-center-starke-in",
            {"op": "observe", "status": "paused", "as_of": "2026-09-02"},
            "the record observes paused on 2026-09-02 already (stale",
        ),
        (
            "aigridwatch_id:abei-energy-data-center-starke-in",
            {"op": "replace_event", "match": {"status": "paused"}, "set": {"status": "paused"}},
            "has the values replace_event sets (stale",
        ),
        (
            "epoch_name:Meta Aiken",
            {"op": "support", "event": {"event": "first_reported"}},
            "cites https://www.postandcourier.com/x already (stale",
        ),
        (
            "osm:way/818167571",
            {"op": "support", "path": "/location/street", "from": "1623 Farnam St"},
            "/location/street is '1623 Farnam Street', not '1623 Farnam St' (stale",
        ),
        (
            "osm:way/818167571",
            {"op": "remove_event", "match": {"event": "other", "status": "operating"}},
            "the corrections leave no event that has happened",
        ),
        (
            "osm:way/818167571",
            {"op": "set", "path": "/capacity/it_mw", "from": None, "value": 20000},
            "fails validation: /capacity/it_mw [range] it_mw 20000.0 is outside (0, 10000]",
        ),
    ],
)
def test_a_stale_or_bad_op_stops_the_run(
    correct: Correct, key: str, op: dict[str, Any], message: str
) -> None:
    record = seed(key)
    fixes = [fix("https://www.postandcourier.com/x", "a quote", op)]
    if op.get("op") == "support" and "event" in op:
        # The same page cited twice for one event: the second support is stale.
        fixes = [
            fix("https://www.postandcourier.com/x", "a quote", op),
            fix("https://www.postandcourier.com/x", "a quote", op),
        ]
    with pytest.raises(FetchError) as e:
        correct(record, *fixes)
    assert message in str(e.value)
    assert str(e.value).startswith(f"{FILE} entry 'k'")


def test_an_event_match_must_find_one_event(correct: Correct) -> None:
    with pytest.raises(FetchError, match=r"2 events match event='approved', not 1 \(stale"):
        correct(
            seed("aigridwatch_id:burkhalter-road-statesboro-ga"),
            fix(
                "https://www.wtoc.com/x",
                "a quote",
                {
                    "op": "add_event",
                    "event": "approved",
                    "status": "permitted",
                    "as_of": "2026-08-18",
                },
                {
                    "op": "add_event",
                    "event": "approved",
                    "status": "permitted",
                    "as_of": "2026-08-20",
                },
                {"op": "remove_event", "match": {"event": "approved"}},
            ),
        )


def test_set_location_at_a_point(correct: Correct, counties: CountyIndex) -> None:
    """A point and an address from a cited page: OSM's s1, which stated each location value,
    states none of them now, and the derived county meta goes with them."""
    before = seed("osm:node/9669340912")
    assert "/location/street" in before.sources[0].supports
    rec = correct(
        before,
        fix(
            "https://www.peeringdb.com/fac/1",
            "111 Market Place, Baltimore, MD 21202",
            {
                "op": "set_location",
                "from": {"postcode": "21202-4036"},
                "state_abbr": "MD",
                "county_fips": "24510",
                "city": "Baltimore",
                "lat": 39.288,
                "lon": -76.606,
                "precision": "address",
                "street": "111 Market Place",
                "postcode": "21202",
            },
            publisher="PeeringDB",
            source_type="commercial_aggregator",
        ),
    ).record
    loc = rec.location
    assert (loc.lat, loc.lon, loc.precision, loc.street, loc.postcode, loc.geometry_ref) == (
        39.288,
        -76.606,
        "address",
        "111 Market Place",
        "21202",
        None,
    )
    assert (loc.geocode_method, loc.county_name) == ("manual", "Baltimore")
    assert rec.canonical_name == before.canonical_name
    assert not any(p.startswith("/location") for p in source(rec, "s1")["supports"])
    assert source(rec, "s3")["supports"] == ["/location"]
    assert [k for k in rec.field_meta if k.startswith("/location")] == ["/location"]
    fm = meta(rec, "/location")
    assert (fm["confidence"], fm["conflicts"][0]["source_ids"]) == (0.65, ["s1"])
    assert valid(rec, counties)


def test_status_reason_needs_the_final_status(correct: Correct) -> None:
    """A denial with local_denial followed by a later approval: the status is permitted, so the
    denial's reason cannot be the record's."""
    with pytest.raises(FetchError, match="status_reason local_denial is for denied, but the"):
        correct(
            seed("aigridwatch_id:burkhalter-road-statesboro-ga"),
            fix(
                "https://www.wtoc.com/x",
                "a quote",
                {
                    "op": "add_event",
                    "event": "denied",
                    "status": "denied",
                    "as_of": "2026-08-18",
                    "status_reason": "local_denial",
                },
                {
                    "op": "add_event",
                    "event": "approved",
                    "status": "permitted",
                    "as_of": "2026-08-19",
                },
            ),
        )


def test_an_active_status_clears_status_reason(correct: Correct) -> None:
    """King of Prussia, approved after its denial (hypothetical): the record is permitted, so the
    importer's local_denial goes."""
    rec = correct(
        seed("aigridwatch_id:king-of-prussia-mlp-pa"),
        fix(
            "https://www.fox29.com/x",
            "a quote",
            {"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2026-09-01"},
        ),
    ).record
    assert (rec.status, rec.status_reason) == ("permitted", None)


def test_a_record_whose_name_lacks_its_place_cannot_be_corrected(correct: Correct) -> None:
    doc = record_json(seed("osm:way/818167571"))
    doc["canonical_name"] = "1623 Farnam"
    with pytest.raises(FetchError, match="does not end with its place ' \\(Omaha, NE\\)'"):
        correct(FacilityRecord.model_validate(doc), fix("https://x.test/", "q", {"op": "hold"}))


# ---------------------------------------------------------------------------- order and repeats


def test_applying_twice_gives_the_same_record(correct: Correct) -> None:
    def once() -> dict[str, Any]:
        applied = correct(
            seed("osm:node/9669340912"),
            fix(
                "https://www.crowncastle.com/directory-page",
                "We've sold our Fiber Solutions business to Zayo",
                {
                    "op": "set_party",
                    "role": "operator",
                    "from": ["Crown Castle Fiber"],
                    "names": ["Zayo"],
                },
                {"op": "rename", "from": "Crown Castle Baltimore", "base": "Zayo Baltimore"},
                source_type="company_release",
            ),
        )
        return record_json(applied.record)

    assert once() == once()


def test_entries_apply_in_order(counties: CountyIndex, sample_gazetteer: Gazetteer) -> None:
    """Two entries that rename one record: the second's from must be the first's base, so one
    decision per record is forced."""
    record = seed("osm:way/793622740")

    def rename(old: str, new: str) -> list[Fix]:
        return FIXES.validate_python(
            [fix("https://x.test/", "q", {"op": "rename", "from": old, "base": new})]
        )

    def run(entries: list[tuple[str, list[Fix]]]) -> FacilityRecord:
        return apply_fixes(
            record,
            entries,
            file=FILE,
            source="test",
            today=TODAY,
            counties=counties,
            gazetteer=sample_gazetteer,
        ).record

    rec = run(
        [
            ("way/793622740", rename("Aligned IAD-02", "Aligned IAD-01 and IAD-02")),
            ("way/1088241977", rename("Aligned IAD-01 and IAD-02", "Aligned Ashburn")),
        ]
    )
    assert rec.canonical_name == "Aligned Ashburn (Ashburn, VA)"
    with pytest.raises(FetchError, match=r"entry 'way/1088241977': corrections\[0\]\.ops\[0\]"):
        run(
            [
                ("way/793622740", rename("Aligned IAD-02", "Aligned IAD-01 and IAD-02")),
                ("way/1088241977", rename("Aligned IAD-02", "Aligned Ashburn")),
            ]
        )


def test_a_rename_drops_an_alias_that_is_the_new_name(correct: Correct) -> None:
    rec = correct(
        seed("osm:way/793622740"),
        fix(
            "https://x.test/",
            "q",
            {"op": "rename", "from": "Aligned IAD-02", "base": "Aligned IAD-01"},
        ),
    ).record
    assert [a.name for a in rec.aliases] == ["IAD"]
    assert rec.canonical_name == "Aligned IAD-01 (Ashburn, VA)"


def test_remove_an_alias_remaps_pointers(correct: Correct) -> None:
    before = seed("osm:way/793622740")
    assert [a.name for a in before.aliases] == ["Aligned IAD-01", "IAD"]
    rec = correct(
        before, fix("https://x.test/", "q", {"op": "remove_alias", "name": "Aligned IAD-01"})
    )
    assert [(a.name, a.source_ids) for a in rec.record.aliases] == [
        ("IAD", before.aliases[1].source_ids)
    ]
    assert changes(rec.record) == [("/aliases", "Aligned IAD-01", None)]


def test_name_suffix(sample_gazetteer: Gazetteer) -> None:
    def suffix(**loc: Any) -> str:
        return name_suffix({"state_abbr": "VA", **loc}, sample_gazetteer)

    assert suffix(city="Ashburn", county_fips="51107") == " (Ashburn, VA)"
    assert suffix(municipality="Dumfries") == " (Dumfries, VA)"
    assert suffix(county_fips="51107", county_name="Loudoun") == " (Loudoun County, VA)"
    assert suffix() == " (VA)"


# ---------------------------------------------------------------------------- the importers' fixtures


def test_ops_on_an_osm_fixture_record(
    make_test_context: MakeContext, correct: Correct, counties: CountyIndex
) -> None:
    parser = argparse.ArgumentParser()
    OsmImporter().add_arguments(parser)
    args = parser.parse_args(
        ["--no-pnnl", "--overrides", str(FIXTURES / "osm" / "overrides-empty.json")]
    )
    built = OsmImporter().run(
        make_test_context(input_path=FIXTURES / "osm" / "overpass-sample.json"), args
    )
    (candidate,) = [c for c in built.candidates if "way/460053028" in c.match_values]
    before = candidate.record
    rec = correct(
        before,
        fix(
            "https://example.org/campus",
            "The campus at 21571 Beaumeade Circle",
            {
                "op": "set",
                "path": "/location/street",
                "from": before.location.street,
                "value": "21571 Beaumeade Circle",
            },
            {"op": "add_party", "role": "tenant", "name": "Example Cloud"},
            source_type="company_release",
        ),
    ).record
    assert rec.location.street == "21571 Beaumeade Circle"
    assert rec.parties.tenant[-1].source_ids == [rec.sources[-1].id]
    assert valid(rec, counties)


def test_ops_on_an_epoch_fixture_record(
    make_test_context: MakeContext, correct: Correct, tmp_path: Path, counties: CountyIndex
) -> None:
    empty = tmp_path / "epoch.json"
    empty.write_text("{}\n", encoding="utf-8")
    built = epoch.IMPORTER.run(
        make_test_context(input_path=FIXTURES / "epoch" / "data_centers.zip"),
        argparse.Namespace(overrides=empty, no_geocode=True),
    )
    (candidate,) = built.candidates
    before = candidate.record
    assert before.canonical_name.startswith("Amazon Madison Mega Site")
    rec = correct(
        before,
        fix(
            "https://example.org/madison",
            "Amazon's Madison County project, first reported in 2022",
            {
                "op": "first_report",
                "from": before.dates["first_reported"].value,
                "as_of": "2022",
                "status": "announced",
            },
            {"op": "add_alias", "name": "Project Example", "kind": "codename"},
        ),
    ).record
    assert (rec.dates["first_reported"].value, rec.dates["first_reported"].precision) == (
        "2022",
        "year",
    )
    assert "/aliases" not in source(rec, "s1")["supports"]
    assert valid(rec, counties)


def test_ops_on_an_aigridwatch_fixture_record(
    make_test_context: MakeContext, correct: Correct, counties: CountyIndex
) -> None:
    built = aigridwatch.IMPORTER.run(
        make_test_context(input_path=FIXTURES / "aigridwatch" / "projects.json"),
        # No committed entry: they are fix round 6's, for the seed's rows, read after this fixture.
        argparse.Namespace(
            without_epoch=True, overrides=FIXTURES / "aigridwatch" / "overrides-none.json"
        ),
    )
    candidate = next(c for c in built.candidates if c.record.status == "proposed")
    before = candidate.record
    rec = correct(
        before,
        fix(
            "https://example.org/denied",
            "The board denied the rezoning",
            {
                "op": "add_event",
                "event": "denied",
                "status": "denied",
                "as_of": "2026-10-01",
                "status_reason": "local_denial",
            },
        ),
    ).record
    assert (rec.status, rec.status_reason) == ("denied", "local_denial")
    assert "/status_history" not in source(rec, "s1")["supports"]
    assert valid(rec, counties)


# ---------------------------------------------------------------------------- places


def test_place_location_is_epochs_override_rule(
    counties: CountyIndex, sample_gazetteer: Gazetteer
) -> None:
    ov = epoch.LocationOverride.model_validate(
        {
            "state_abbr": "PA",
            "county_fips": "42079",
            "municipality": "Salem Township",
            "precision": "locality",
            "source_url": "https://example.org/berwick",
            "quote": "Salem Township, Luzerne County",
            "retrieved_at": "2026-10-10T12:00:00Z",
            "note": "the township is stated",
        }
    )
    placed = epoch.place_override("AWS Berwick", ov, counties=counties, gazetteer=sample_gazetteer)
    location, label = place_location(ov, where="x", counties=counties, gazetteer=sample_gazetteer)
    assert (placed.location, placed.city_or_county) == (location, label)
    assert label == "Salem Township"
    bad = ov.model_copy(update={"county_fips": "42037"})
    with pytest.raises(FetchError, match=r"^where: no Gazetteer county subdivision"):
        place_location(bad, where="where", counties=counties, gazetteer=sample_gazetteer)


# ---------------------------------------------------------------------------- the entries


def held_item(key: str) -> ReviewItem:
    return ReviewItem(
        source="test",
        kind="unknown_status",
        external_id="other",
        reason="the row's stage is ahead of its log",
        data={"refs": [key]},
    )


def test_check_entries() -> None:
    (hold,) = FIXES.validate_python([fix("https://x.test/", "q", {"op": "hold"})])
    (move,) = FIXES.validate_python(
        [
            fix(
                "https://x.test/",
                "q",
                {"op": "set", "path": "/purpose", "from": "unknown", "value": "colocation"},
            )
        ]
    )
    entries: dict[str, list[Fix]] = {"a": [move], "b": [hold], "c": []}
    check_entries(entries, present={"a", "b", "c"}, built={"a"}, review=[], file=FILE)
    with pytest.raises(FetchError, match=f"^{FILE} names 'd', which this input does not have"):
        check_entries(
            {**entries, "d": []}, present={"a", "b", "c"}, built={"a"}, review=[], file=FILE
        )
    with pytest.raises(FetchError) as e:
        check_entries(
            entries, present={"a", "b", "c"}, built=set(), review=[held_item("a")], file=FILE
        )
    assert str(e.value) == (
        f"{FILE} entry 'a': no record was built for a (held: unknown_status: the row's stage is "
        "ahead of its log); release the check or remove the correction"
    )


def test_mark_corrected_and_metrics() -> None:
    parsed = FIXES.validate_python(
        [
            fix(
                "https://x.test/", "q", {"op": "hold"}, {"op": "out_of_scope"}, basis="importer_gap"
            ),
            fix(
                "https://x.test/",
                "q",
                {"op": "set", "path": "/purpose", "from": "unknown", "value": "colocation"},
            ),
        ]
    )
    entries = {"a": parsed, "b": []}
    item = ReviewItem(source="test", kind="conflict", external_id="a", reason="r", data={"x": 1})
    other = item.model_copy(update={"external_id": "z"})
    marked = mark_corrected([item, other], entries, file=FILE)
    assert marked[0].data == {"x": 1, "corrected_by": f"{FILE} (reviewed 2026-10-10): {REASON}"}
    assert marked[1] == other
    assert correction_metrics(entries, corrected=1) == {
        "corrections_entries": 1,
        "corrections_ops": 3,
        "records_corrected": 1,
        "corrections_held": 1,
        "corrections_flagged": 0,  # the flag op of the round-6 review (entries-4)
        "corrections_out_of_scope": 1,
        "corrections_importer_gap": 1,
    }


# ---------------------------------------------------------------------------- merge_import


class _Importer:
    name = "dummy"
    match_key = "dummy_id"
    owned_external_keys = ("dummy_id",)
    review_sources = ("dummy",)
    version = "1"
    help = "a test importer"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        raise NotImplementedError


def merge(
    tmp_repo: Path,
    ctx: ImportContext,
    candidates: list[Candidate],
    review: list[ReviewItem] | None = None,
) -> tuple[ImportReceipt, list[ReviewItem]]:
    return merge_import(
        _Importer(),
        ImportResult("dummy", [], candidates, review or []),
        ctx,
        store=RecordStore(tmp_repo / "data" / "records"),
        review_dir=tmp_repo / "review" / "queue",
        receipts_dir=tmp_repo / "data" / "imports",
    )


MakeRecord = Callable[..., FacilityRecord]


def candidate(make_record: MakeRecord, *values: str, **kw: Any) -> Candidate:
    joined = kw.pop("joined", ())
    split_from = kw.pop("split_from", None)
    record = make_record(id=PLACEHOLDER_ID, external_ids={"dummy_id": list(values)}, **kw)
    return Candidate(values, record, joined=joined, split_from=split_from)


def note(pointer: str, new: Any) -> Correction:
    return Correction(on=date(2026, 10, 10), pointer=pointer, old=None, new=new, reason=REASON)


LATER = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def test_corrections_are_an_append_only_union(
    tmp_repo: Path, make_record: MakeRecord, make_test_context: MakeContext
) -> None:
    store = RecordStore(tmp_repo / "data" / "records")
    c1, c2 = note("/aliases", "A"), note("/aliases", "B")
    stored = make_record(external_ids={"dummy_id": ["a"]}, corrections=[record_json(c1)])
    store.write(stored)
    ctx = make_test_context(now=LATER, today=LATER.date())
    receipt, _ = merge(
        tmp_repo, ctx, [candidate(make_record, "a", corrections=[record_json(c1), record_json(c2)])]
    )
    assert receipt.counts["updated"] == 1
    assert store.load()[stored.id].corrections == [c1, c2]
    # The same corrections again: unchanged. None (an override withdrawn): the history stays.
    for again in ([record_json(c1), record_json(c2)], []):
        receipt, _ = merge(tmp_repo, ctx, [candidate(make_record, "a", corrections=again)])
        assert receipt.counts["unchanged"] == 1
        assert store.load()[stored.id].corrections == [c1, c2]


def joined_store(
    tmp_repo: Path,
    make_record: MakeRecord,
    make_test_context: MakeContext,
    *,
    older_first: bool = False,
) -> tuple[RecordStore, FacilityRecord, FacilityRecord]:
    """Two stored records, a (created first) and b. Ids increase as records are made, so with
    older_first=False the older record has the larger id: created_at decides (07 §6.6)."""
    store = RecordStore(tmp_repo / "data" / "records")

    def make_older() -> FacilityRecord:
        return make_record(
            external_ids={"dummy_id": ["a"]},
            canonical_name="A (Ashburn, VA)",
            created_at="2026-01-01T00:00:00Z",
        )

    first = make_older() if older_first else None
    younger = make_record(external_ids={"dummy_id": ["b"]}, canonical_name="B (Ashburn, VA)")
    older = first or make_older()
    assert (older.id < younger.id) == older_first
    store.write(younger)
    store.write(older)
    return store, older, younger


@pytest.mark.parametrize("older_first", [False, True])
def test_a_reviewed_join_merges_stored_records(
    tmp_repo: Path,
    make_record: MakeRecord,
    make_test_context: MakeContext,
    counties: CountyIndex,
    older_first: bool,
) -> None:
    store, older, younger = joined_store(
        tmp_repo, make_record, make_test_context, older_first=older_first
    )
    ctx = make_test_context(now=LATER, today=LATER.date())
    # Without a reviewed join, two records for one candidate stay a conflict.
    receipt, review = merge(tmp_repo, ctx, [candidate(make_record, "a", "b")])
    assert receipt.counts["conflicts"] == 1 and [i.kind for i in review] == ["conflict"]

    joined = candidate(
        make_record, "a", "b", joined=(("a",), ("b",)), canonical_name="A and B (Ashburn, VA)"
    )
    receipt, review = merge(tmp_repo, ctx, [joined])
    assert (receipt.counts["updated"], receipt.counts["merged"], receipt.counts["conflicts"]) == (
        1,
        1,
        0,
    )
    assert review == []
    after = store.load()
    survivor, merged = after[older.id], after[younger.id]
    assert survivor.canonical_name == "A and B (Ashburn, VA)"
    assert survivor.external_ids["dummy_id"] == ["a", "b"] and survivor.merged_into is None
    assert merged.merged_into == older.id and merged.updated_at == LATER
    assert merged.corrections[-1].pointer == "/merged_into"
    assert (merged.corrections[-1].old, merged.corrections[-1].new) == (None, older.id)
    for r in (survivor, merged):
        assert validate_record(r, counties=counties, today=LATER.date()) == []

    # The next run: the index prefers the survivor, so no conflict and nothing is removed.
    receipt, review = merge(tmp_repo, ctx, [joined])
    assert receipt.counts["unchanged"] == 1
    assert (receipt.counts["conflicts"], receipt.counts["removed_upstream"]) == (0, 0)
    assert receipt.counts["merged"] == 0 and review == []


def test_a_join_does_not_merge_a_reviewed_record(
    tmp_repo: Path, make_record: MakeRecord, make_test_context: MakeContext
) -> None:
    store, _, younger = joined_store(tmp_repo, make_record, make_test_context)
    store.write(
        FacilityRecord.model_validate(
            record_json(younger) | {"review": {"state": "human_reviewed"}}
        )
    )
    ctx = make_test_context(now=LATER, today=LATER.date())
    receipt, review = merge(
        tmp_repo, ctx, [candidate(make_record, "a", "b", joined=(("a",), ("b",)))]
    )
    assert receipt.counts["conflicts"] == 1 and receipt.counts["merged"] == 0
    assert review[0].reason.endswith("is reviewed or merged: merge them in a reviewed PR")
    assert store.load()[younger.id].merged_into is None


def test_a_split_unmerges(
    tmp_repo: Path, make_record: MakeRecord, make_test_context: MakeContext
) -> None:
    store, older, younger = joined_store(tmp_repo, make_record, make_test_context)
    ctx = make_test_context(now=LATER, today=LATER.date())
    merge(tmp_repo, ctx, [candidate(make_record, "a", "b", joined=(("a",), ("b",)))])
    # The reviewer splits b out again (OSM's alone): the merged record takes it back.
    split = [candidate(make_record, "a"), candidate(make_record, "b", split_from="b")]
    receipt, review = merge(tmp_repo, ctx, split)
    assert (receipt.counts["updated"], receipt.counts["new"], receipt.counts["conflicts"]) == (
        2,
        0,
        0,
    )
    after = store.load()
    assert after[younger.id].merged_into is None
    assert after[younger.id].corrections[-1].pointer == "/merged_into"
    assert after[younger.id].corrections[-1].old == older.id
    assert after[older.id].external_ids["dummy_id"] == ["a"]
    receipt, review = merge(tmp_repo, ctx, split)
    assert receipt.counts["unchanged"] == 2 and review == []


def test_a_split_without_a_merged_record_is_new(
    tmp_repo: Path, make_record: MakeRecord, make_test_context: MakeContext
) -> None:
    store = RecordStore(tmp_repo / "data" / "records")
    both = make_record(external_ids={"dummy_id": ["a", "c"]})
    store.write(both)
    ctx = make_test_context(now=LATER, today=LATER.date())
    split = [candidate(make_record, "c", split_from="c"), candidate(make_record, "a")]
    receipt, _ = merge(tmp_repo, ctx, split)
    assert (receipt.counts["new"], receipt.counts["updated"], receipt.counts["conflicts"]) == (
        1,
        1,
        0,
    )
    after = store.load()
    assert after[both.id].external_ids["dummy_id"] == ["a"]
    (new,) = [r for r in after.values() if r.id != both.id]
    assert new.external_ids["dummy_id"] == ["c"]
    receipt, _ = merge(tmp_repo, ctx, split)
    assert receipt.counts["unchanged"] == 2


def test_a_hold_unpublishes_a_stored_record(
    tmp_repo: Path,
    make_record: MakeRecord,
    make_test_context: MakeContext,
    counties: CountyIndex,
    sample_gazetteer: Gazetteer,
) -> None:
    store = RecordStore(tmp_repo / "data" / "records")
    stored = make_record(external_ids={"dummy_id": ["a"]})
    store.write(stored)
    held = apply_fixes(
        candidate(make_record, "a").record,
        [("a", FIXES.validate_python([fix("https://x.test/", "q", {"op": "hold"})]))],
        file=FILE,
        source="dummy",
        today=TODAY,
        counties=counties,
        gazetteer=sample_gazetteer,
    )
    ctx = make_test_context(now=LATER, today=LATER.date())
    receipt, review = merge(tmp_repo, ctx, [Candidate(("a",), held.record)], held.review)
    assert receipt.counts["updated"] == 1 and receipt.counts["removed_upstream"] == 0
    after = store.load()[stored.id]
    assert after.scope == "out_of_scope"
    assert [c.pointer for c in after.corrections] == ["/scope"]
    assert [i.kind for i in review] == ["conflict"]


def test_candidate_join_and_split_values(make_record: MakeRecord) -> None:
    record = make_record(id=PLACEHOLDER_ID)
    with pytest.raises(ValueError, match="joined values must be among"):
        Candidate(("a",), record, joined=(("b",),))
    with pytest.raises(ValueError, match="joined parts must be non-empty and disjoint"):
        Candidate(("a", "b"), record, joined=(("a", "b"), ("b",)))
    with pytest.raises(ValueError, match="joined parts must be non-empty and disjoint"):
        Candidate(("a", "b"), record, joined=(("a",), ()))
    with pytest.raises(ValueError, match="split_from must be one of"):
        Candidate(("a",), record, split_from="b")


# ---------------------------------------------------------------------------- the review of round 6
# Each test is a confirmed finding of the review of fix round 6 (its id first), on its reproduction.

WICHITA = fix(
    "https://www.newschannel6now.com/2026/08/13/datanovax-data-center-approved/",
    "the Planning and Zoning Commission approved a conditional-use request",
    {"op": "remove_event", "match": {"event": "other", "status": "operating"}},
    {"op": "add_event", "event": "approved", "status": "permitted", "as_of": "2026-08"},
)
KOP_REDATE = fix(
    "https://www.fox29.com/news/upper-merion-board-supervisors-denies",
    "denied all five proposed data center sites by MLP Ventures on Thursday, August 13.",
    {
        "op": "replace_event",
        "match": {"event": "denied", "as_of": "2026-08-14"},
        "set": {"as_of": "2026-08-13"},
    },
)


def test_lost_events_reads_what_corrections_removed(correct: Correct) -> None:
    """mechanism-0: the events a stored record's corrections removed or redated, with their
    sources, from field_meta /status_history conflicts (an importer keeps an observation's
    first-seen date by them)."""
    assert lost_events(seed("osm:way/747985149")) == []
    removed = correct(seed("osm:way/747985149"), WICHITA).record
    (lost,) = lost_events(removed)
    assert (lost.event, lost.status, lost.phase_id, lost.planned, lost.source_ids) == (
        "other",
        "operating",
        None,
        False,
        ["s1"],
    )
    assert (lost.as_of.value, lost.as_of.precision) == ("2026-10-09", "day")
    redated = correct(seed("aigridwatch_id:king-of-prussia-mlp-pa"), KOP_REDATE).record
    assert [(e.event, e.as_of.value, e.source_ids) for e in lost_events(redated)] == [
        ("denied", "2026-08-14", ["s1"])
    ]
    # A conflict that is no event summary is not an event.
    d = doc(removed)
    d["field_meta"]["/status_history"]["conflicts"].append({"value": "x", "source_ids": ["s1"]})
    assert lost_events(FacilityRecord.model_validate(d)) == [lost]


def test_a_later_snapshot_gives_the_same_correction(
    tmp_repo: Path, make_test_context: MakeContext, correct: Correct
) -> None:
    """mechanism-0: OSM dates its observation with each snapshot, so the same remove_event on a
    later snapshot summarises an event of a later date. Once the importer keeps the observation's
    first-seen date from lost_events, the record is unchanged; and merge_import's union never
    appends the same decision with a later observation date (corrections[] stays as it was)."""

    def snapshot(day: str, stored: FacilityRecord | None = None) -> Candidate:
        d = doc(seed("osm:way/747985149"))
        d["external_ids"]["dummy_id"] = ["w"]
        d["status_history"][0]["as_of"] = {"value": day, "precision": "day"}
        if stored is not None:  # the importer keeps the first-seen date (osm, aigridwatch)
            (first,) = [e for e in lost_events(stored) if e.status == "operating"]
            d["status_history"][0]["as_of"] = first.as_of.model_dump()
        return Candidate(("w",), correct(FacilityRecord.model_validate(d), WICHITA).record)

    store = RecordStore(tmp_repo / "data" / "records")
    ctx = make_test_context(now=LATER, today=LATER.date())
    receipt, _ = merge(tmp_repo, ctx, [snapshot("2026-10-09")])
    assert receipt.counts["new"] == 1
    ((rid, week1),) = store.load().items()
    receipt, _ = merge(tmp_repo, ctx, [snapshot("2026-10-16", week1)])
    assert receipt.counts["unchanged"] == 1
    # An importer that dates the observation anew: the record changes (its conflict's date), the
    # history does not grow.
    receipt, _ = merge(tmp_repo, ctx, [snapshot("2026-10-23")])
    after = store.load()[rid]
    assert receipt.counts["updated"] == 1
    assert after.corrections == week1.corrections and len(after.corrections) == 2
    (lost,) = lost_events(after)
    assert lost.as_of.value == "2026-10-23"


@pytest.mark.parametrize("b_first", [False, True])
def test_a_withdrawn_join_unmerges(
    tmp_repo: Path, make_record: MakeRecord, make_test_context: MakeContext, b_first: bool
) -> None:
    """mechanism-1: a reviewed join merged b's record into a's on a populated store; the
    reviewer withdraws the join, so b's element is a candidate of its own again. b's record is
    taken back and unmerged, in either candidate order: both facilities keep their ids and their
    own values, and both are published."""
    store, older, younger = joined_store(tmp_repo, make_record, make_test_context)
    ctx = make_test_context(now=LATER, today=LATER.date())
    receipt, _ = merge(tmp_repo, ctx, [candidate(make_record, "a", "b", joined=(("a",), ("b",)))])
    assert receipt.counts["merged"] == 1
    plain = [
        candidate(make_record, "a", canonical_name="A (Ashburn, VA)"),
        candidate(make_record, "b", canonical_name="B (Ashburn, VA)"),
    ]
    if b_first:
        plain.reverse()
    receipt, review = merge(tmp_repo, ctx, plain)
    assert (receipt.counts["updated"], receipt.counts["conflicts"], receipt.counts["held"]) == (
        2,
        0,
        0,
    )
    assert review == []
    after = store.load()
    assert set(after) == {older.id, younger.id}
    a, b = after[older.id], after[younger.id]
    assert (a.canonical_name, a.external_ids["dummy_id"], a.merged_into) == (
        "A (Ashburn, VA)",
        ["a"],
        None,
    )
    assert (b.canonical_name, b.external_ids["dummy_id"], b.merged_into) == (
        "B (Ashburn, VA)",
        ["b"],
        None,
    )
    assert (b.corrections[-1].pointer, b.corrections[-1].old, b.corrections[-1].new) == (
        "/merged_into",
        older.id,
        None,
    )
    assert b.corrections[-1].reason == (
        f"unmerged from {older.id}: no reviewed join brings b into that record any more"
    )
    assert a.scope == b.scope == "in_scope"
    for _ in range(2):
        receipt, review = merge(tmp_repo, ctx, plain)
        assert receipt.counts["unchanged"] == 2 and review == []


def test_a_merge_into_a_record_without_the_values_stays(
    tmp_repo: Path, make_record: MakeRecord, make_test_context: MakeContext
) -> None:
    """mechanism-1's guard: a merge a reviewer made into another source's record (which holds
    none of this importer's values) is no join to withdraw: the candidate stays held."""
    store = RecordStore(tmp_repo / "data" / "records")
    epoch = make_record(external_ids={"epoch_name": ["X"]})
    merged = make_record(external_ids={"dummy_id": ["b"]}, merged_into=epoch.id)
    store.write(epoch)
    store.write(merged)
    ctx = make_test_context(now=LATER, today=LATER.date())
    receipt, review = merge(tmp_repo, ctx, [candidate(make_record, "b")])
    assert receipt.counts["held"] == 1 and [i.kind for i in review] == ["held_merged"]
    assert store.load()[merged.id].merged_into == epoch.id


@pytest.mark.parametrize(
    "change",
    [
        {"match": {"event": "denied"}, "set": {"as_of": "2026-08-13"}},
        {"match": {"status": "denied", "as_of": "2026-08-14"}, "set": {"as_of": "2026-08-13"}},
        {
            "match": {"event": "first_reported"},
            "set": {"event": "cancelled", "status": "cancelled"},
        },
    ],
)
def test_gate_6_holds_for_replace_event_at_load(change: dict[str, Any]) -> None:
    """mechanism-2: a replace_event that gives a paused, denied or cancelled status citing the
    Fix alone (a new stopped status, or a new date of a stopped event) needs a gate 6 source."""
    op = {"op": "replace_event", **change}
    for kind in ("community_report", "commercial_aggregator"):
        with pytest.raises(ValidationError, match=re.escape("(07 §6.4 gate 6)")):
            FIXES.validate_python([with_op(op, source_type=kind)])
    FIXES.validate_python([with_op(op, source_type="news")])
    # A retype or a note keeps the event's sources: no gate.
    FIXES.validate_python(
        [with_op({**op, "set": {"note": "per the minutes"}}, source_type="community_report")]
    )


def test_gate_6_holds_for_replace_event_when_applied(correct: Correct) -> None:
    """mechanism-2: King of Prussia's denial as an observation (an `other` event): the match names
    no stopped status, so only the event itself shows the redate gives a denial citing the Fix
    alone, when the op is applied."""
    d = doc(seed("aigridwatch_id:king-of-prussia-mlp-pa"))
    d["status_history"][1]["event"] = "other"
    record = FacilityRecord.model_validate(d)
    redate = {
        "op": "replace_event",
        "match": {"event": "other", "as_of": "2026-08-14"},
        "set": {"as_of": "2026-08-13"},
    }
    reddit = "https://www.reddit.com/r/kingofprussia/comments/x/"
    with pytest.raises(FetchError, match=re.escape("not community_report (07 §6.4 gate 6)")):
        correct(record, fix(reddit, "voted it down", redate, source_type="community_report"))
    assert correct(record, fix(reddit, "voted it down", redate)).record.status == "denied"
    noted = correct(
        seed("aigridwatch_id:king-of-prussia-mlp-pa"),
        fix(
            reddit,
            "voted it down",
            {"op": "replace_event", "match": {"event": "denied"}, "set": {"note": "Per the vote"}},
            source_type="community_report",
        ),
    ).record
    assert events(noted)[1] == ("denied", "denied", "2026-08-14", None, ["s1", "s3"])


def test_removing_the_deciding_event_rebuilds_the_status_meta(correct: Correct) -> None:
    """mechanism-3: CyrusOne Cincinnati-Lebanon's operating phase decides its status. A
    remove_event of that observation leaves the under-construction phase to decide it, and
    field_meta /status follows: the importer's method and confidence, the new event's sources."""
    before = seed("osm:way/95349317")
    assert meta(before, "/status") == {
        "confidence": 0.6,
        "method": "imported",
        "source_ids": ["s3", "s4"],
        "conflicts": [],
    }
    remove = fix(
        "https://example.org/cyrusone",
        "the operating hall is not built yet",
        {
            "op": "remove_event",
            "match": {"event": "other", "status": "operating", "phase": "osm-operating"},
        },
    )
    rec = correct(before, remove).record
    assert rec.status == "under_construction"
    assert events(rec) == [
        ("other", "under_construction", "2026-10-09", "osm-under_construction", ["s5", "s6"])
    ]
    assert meta(rec, "/status") == {
        "confidence": 0.6,
        "method": "imported",
        "source_ids": ["s5", "s6"],
        "conflicts": [],
    }
    # A reviewer's status (OSM's element `status`) for the event that no longer decides goes.
    d = doc(before)
    d["field_meta"]["/status"]["method"] = "human_entered"
    assert "/status" not in correct(FacilityRecord.model_validate(d), remove).record.field_meta


def test_a_status_correction_has_the_conflict_term(correct: Correct) -> None:
    """mechanism-4, entries-2: 07 §3.4 as written on the record ops as on OSM's element status
    (an owner question): a commercial aggregator's status over an event from an open dataset is
    0.95 - 0.10 - 0.20 = 0.65, in field_meta /status and /status_history."""
    baxtel = {"publisher": "Baxtel", "source_type": "commercial_aggregator"}
    # Elk Grove Village: Baxtel over OpenStreetMap's operating observation.
    rec = correct(
        seed("osm:way/747985149"),
        fix(
            "https://baxtel.com/data-center/elk-grove",
            "under construction",
            {"op": "remove_event", "match": {"event": "other", "status": "operating"}},
            {"op": "observe", "status": "under_construction", "as_of": "2026-09"},
            **baxtel,
        ),
    ).record
    assert rec.status == "under_construction"
    assert meta(rec, "/status")["confidence"] == human_confidence(
        "commercial_aggregator", ["open_dataset"]
    )
    assert (meta(rec, "/status")["confidence"], meta(rec, "/status_history")["confidence"]) == (
        0.65,
        0.65,
    )
    # CoreWeave Muskogee: SynMax's observation in place of Epoch AI's planned operation.
    phase = "coreweave-muskogee-ok"
    rec = correct(
        seed("epoch_name:CoreWeave Muskogee OK"),
        fix(
            "https://www.synmax.com/vulcan/muskogee",
            "operating",
            {"op": "observe", "status": "operating", "phase": phase, "as_of": "2026-07-15"},
            {"op": "remove_event", "match": {"event": "energized", "planned": True}},
            publisher="SynMax",
            source_type="commercial_aggregator",
        ),
    ).record
    assert (rec.status, meta(rec, "/status")["confidence"]) == ("operating", 0.65)
    assert meta(rec, "/status_history")["confidence"] == 0.65
    # An observation that replaces no event has no conflict term (add ops).
    rec = correct(
        seed("osm:way/747985149"),
        fix(
            "https://baxtel.com/data-center/elk-grove",
            "under construction",
            {"op": "observe", "status": "under_construction", "as_of": "2026-10-10"},
            **baxtel,
        ),
    ).record
    assert meta(rec, "/status")["confidence"] == 0.85 and "/status_history" not in rec.field_meta


def test_a_phase_capacity_keeps_the_replaced_sources(
    correct: Correct, counties: CountyIndex
) -> None:
    """entries-1: a phase's source_ids support everything in it (07 §3.3 rule b). Dalton's 28 MW
    and 40 MW are Epoch AI's, its phase's s1: the conflicts keep s1. Ellendale's 175 MW comes from
    the cited capacity part s4, which the phase cites for its capacity alone: s4 no longer cites
    the phase (it supports the capacity value beside the corrected one), and the part's
    field_meta goes, its conflicts with the corrected value's."""
    core = {"publisher": "Core Scientific", "source_type": "company_release"}
    phase = "coreweave-dalton-1-2"
    rec = correct(
        seed("epoch_name:CoreWeave Dalton 1 & 2"),
        fix(
            "https://investors.corescientific.com/news-events/1q26-prepared-remarks",
            "At Dalton Phase 1, we likewise achieved full site handover and delivered 30 megawatts",
            {"op": "set", "phase": phase, "path": "/capacity/it_mw", "from": 28.0, "value": 30},
            {
                "op": "set",
                "phase": phase,
                "path": "/capacity/facility_mw",
                "from": 40.0,
                "value": None,
            },
            **core,
        ),
    ).record
    replaced = "replaced by a cited correction"
    assert meta(rec, "/phases/0/capacity/it_mw")["conflicts"] == [
        {"value": 28.0, "source_ids": ["s1"], "note": replaced}
    ]
    assert meta(rec, "/phases/0/capacity/facility_mw")["conflicts"] == [
        {"value": 40.0, "source_ids": ["s1"], "note": replaced}
    ]
    assert rec.phases[0].source_ids == ["s1"]  # Epoch AI's row: the phase itself

    phase = "coreweave-ellendale-nd"
    before = seed("epoch_name:CoreWeave Ellendale ND")
    assert before.phases[0].source_ids == ["s1", "s4"]
    modelled = meta(before, "/phases/0/capacity")["conflicts"]
    assert [c["source_ids"] for c in modelled] == [["s1"]]
    url = "https://ir.applieddigital.com/news-events/press-releases/detail/polaris-forge-1"
    quote = "fully operational critical IT load at Polaris Forge 1 to 250 MW"
    rate = {"op": "set", "phase": phase, "path": "/capacity/it_mw", "from": 175.0, "value": 250}
    words = {
        "op": "set",
        "phase": phase,
        "path": "/capacity/mw_as_stated",
        "from": "175 MW now live",
        "value": quote,
    }
    half = correct(before, fix(url, quote, rate, publisher="Applied Digital")).record
    assert half.phases[0].source_ids == ["s1"]
    assert source(half, "s4")["supports"] == ["/phases/0/capacity/mw_as_stated"]
    assert "/phases/0/capacity" not in half.field_meta
    assert meta(half, "/phases/0/capacity/it_mw")["conflicts"] == [
        {"value": 175.0, "source_ids": ["s1", "s4"], "note": replaced},
        *modelled,
    ]
    full = correct(before, fix(url, quote, rate, words, publisher="Applied Digital")).record
    assert source(full, "s4")["supports"] == []
    assert source(full, "s5")["supports"] == [
        "/phases/0/capacity/it_mw",
        "/phases/0/capacity/mw_as_stated",
    ]
    assert meta(full, "/phases/0/capacity/mw_as_stated")["conflicts"] == [
        {"value": "175 MW now live", "source_ids": ["s1", "s4"], "note": replaced}
    ]
    assert valid(full, counties)


def test_set_location_keeps_the_point_its_source_gives(
    correct: Correct, counties: CountyIndex
) -> None:
    """entries-3: Kearney is outside the city: the record names Buffalo County, at AI
    GridWatch's own point. The point and the method that placed it stay AI GridWatch's (s1 still
    supports lat and lon); only the fields the op changes go to the Fix."""
    before = seed("aigridwatch_id:kearney-data-center-ne")
    assert (before.location.geocode_method, before.location.precision) == (
        "source_coords",
        "locality",
    )
    rec = correct(
        before,
        fix(
            "https://www.ksnblocal4.com/2026/08/21/kearney-planning-commission/",
            "Because Tech oNE Crossing is outside Kearney city limits",
            {
                "op": "set_location",
                "from": {"city": "Kearney", "county_fips": None},
                "state_abbr": "NE",
                "county_fips": "31019",
                "lat": 40.7,
                "lon": -99.08,
                "precision": "county",
            },
        ),
    ).record
    loc = rec.location
    assert (loc.lat, loc.lon, loc.geocode_method, loc.precision, loc.city, loc.county_fips) == (
        40.7,
        -99.08,
        "source_coords",
        "county",
        None,
        "31019",
    )
    s1 = source(rec, "s1")["supports"]
    assert {"/location/lat", "/location/lon", "/location/state_abbr"} <= set(s1)
    assert not any(p in s1 for p in ("/location", "/location/city", "/location/county_fips"))
    assert source(rec, "s3")["supports"] == ["/location/county_fips", "/location/county_name"]
    assert meta(rec, "/location") == meta(before, "/location")  # AI GridWatch's, for the point
    assert meta(rec, "/location/city") == {
        "confidence": 0.95,
        "method": "human_entered",
        "source_ids": ["s3"],
        "conflicts": [
            {"value": "Kearney", "source_ids": ["s1"], "note": "replaced by a cited correction"}
        ],
    }
    assert rec.corrections[-1].old == {
        "city": "Kearney",
        "county_fips": None,
        "county_name": None,
        "precision": "locality",
    }
    assert valid(rec, counties)


def test_flag_files_an_item_and_changes_nothing(correct: Correct) -> None:
    """entries-4: a flag files a review item with the Fix's reason, URL and quote and changes
    nothing else (the record does not cite a page that only flags); counted in the receipt."""
    key = "cloudburst-san-marcos-tx"
    before = seed("aigridwatch_id:burkhalter-road-statesboro-ga")
    url = "https://herald-zeitung.com/news/cloudburst"
    quote = "a data center facility in northern Guadalupe County, bordering Hays County"
    applied = correct(before, fix(url, quote, {"op": "flag"}), key=key)
    assert record_json(applied.record) == record_json(before)
    (item,) = applied.review
    assert (item.source, item.kind, item.external_id, item.record_id) == (
        "test",
        "conflict",
        key,
        None,
    )
    assert item.reason == (
        f"flagged by {FILE} entry '{key}' (reviewed 2026-10-10): {REASON} ({url}); the record is "
        "published as it is, the question stays open for a reviewer"
    )
    assert item.data == {
        "source_url": url,
        "quote": quote,
        "reviewed_at": "2026-10-10",
        "basis": "upstream_error",
        "file": FILE,
    }
    (parsed,) = FIXES.validate_python([fix(url, quote, {"op": "flag", "kind": "unknown_status"})])
    assert correction_metrics({key: [parsed]}, corrected=0)["corrections_flagged"] == 1
    # A flag about a record the importer did not build is stale, like any op but hold.
    with pytest.raises(FetchError, match="no record was built"):
        check_entries({key: [parsed]}, present={key}, built=set(), review=[], file=FILE)


def test_a_join_merges_only_records_in_different_parts(
    tmp_repo: Path, make_record: MakeRecord, make_test_context: MakeContext
) -> None:
    """osm-1 (review of fix round 6): a join's candidate merges the stored records only when each
    lies in parts of its own. a and b in one part were put together by the source's own rules
    (an OSM edit the dissolve follows), not by the reviewer, so that stays a conflict."""
    store = RecordStore(tmp_repo / "data" / "records")
    for value in ("a", "b", "c"):
        store.write(
            make_record(external_ids={"dummy_id": [value]}, canonical_name=f"{value} (Ashburn, VA)")
        )
    ctx = make_test_context(now=LATER, today=LATER.date())
    parts: tuple[tuple[str, ...], ...] = (("a", "b"), ("c",))
    receipt, review = merge(tmp_repo, ctx, [candidate(make_record, "a", "b", "c", joined=parts)])
    assert (receipt.counts["merged"], receipt.counts["conflicts"]) == (0, 1)
    assert [i.reason for i in review] == ["dummy_id values match 3 records"]
    assert all(r.merged_into is None for r in store.load().values())
    parts = (("a",), ("b",), ("c",))
    receipt, review = merge(tmp_repo, ctx, [candidate(make_record, "a", "b", "c", joined=parts)])
    assert (receipt.counts["merged"], receipt.counts["conflicts"]) == (2, 0)


def test_add_event_gives_no_first_report() -> None:
    """epoch-agw-1 (review of fix round 6): an added first_reported event would skip the
    first_report op's guards (from, earlier than every dated event) and its count, which keeps an
    Epoch first_report part and an AI GridWatch first_reported from dating a second first
    report. A first report is the first_report op."""
    added = {"op": "add_event", "event": "first_reported", "status": "announced", "as_of": "2024"}
    with pytest.raises(ValidationError, match="add_event: a first report is the first_report op"):
        FIXES.validate_python([with_op(added)])
    FIXES.validate_python([with_op({**added, "event": "announced"})])


@pytest.mark.parametrize(
    ("key", "change", "refused"),
    [
        # Meta Aiken: first_reported 2023-03-22, construction_start 2024-05-30.
        ("epoch_name:Meta Aiken", {"as_of": "2024-06-01"}, "construction_start on 2024-05-30"),
        ("epoch_name:Meta Aiken", {"as_of": "2024-05-30"}, "construction_start on 2024-05-30"),
        ("epoch_name:Meta Aiken", {"as_of": "2023-01"}, None),
        ("epoch_name:Meta Aiken", {"note": "First report, per the county"}, None),
        # Amazon Bridgefield: announced 2026-03-17, application_filed 2026-07-31.
        ("aigridwatch_id:amazon-bridgefield-ashburn-va", {"event": "first_reported"}, None),
    ],
)
def test_a_replace_event_that_gives_a_first_report_is_earlier_than_every_event(
    correct: Correct, key: str, change: dict[str, Any], refused: str | None
) -> None:
    """epoch-agw-1: a replace_event whose result is first_reported, with a new date or a retype
    (STACK Berry Hill's announcement that is a first report), is held to the first_report op's
    guard: it is earlier than every other dated event. A note leaves it as it is."""
    first = "first_reported" if key.startswith("epoch") else "announced"
    op = {"op": "replace_event", "match": {"event": first}, "set": change}
    url = "https://example.org/report"
    if refused is not None:
        with pytest.raises(FetchError, match=f"is not earlier than the record's {refused}"):
            correct(seed(key), fix(url, "a quote", op))
        return
    rec = correct(seed(key), fix(url, "a quote", op)).record
    assert events(rec)[0][0] == "first_reported"
    assert rec.dates["first_reported"].value == events(rec)[0][2]


def test_a_retype_to_a_first_report_after_another_event_is_refused(correct: Correct) -> None:
    # epoch-agw-1: Amazon Bridgefield's filing (2026-07-31) is later than its announcement.
    op = {
        "op": "replace_event",
        "match": {"event": "application_filed"},
        "set": {"event": "first_reported"},
    }
    with pytest.raises(FetchError, match="is not earlier than the record's announced on 2026-03"):
        correct(
            seed("aigridwatch_id:amazon-bridgefield-ashburn-va"),
            fix("https://example.org/report", "a quote", op),
        )
