from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from atlas.schema.pointers import (
    EXEMPT_PREFIXES,
    claim_pointers,
    escape,
    is_supported,
    resolve,
    split,
    unsupported_pointers,
)
from atlas.schema.record import FacilityRecord

MakeRecord = Callable[..., FacilityRecord]


def test_escape_split_and_resolve() -> None:
    assert escape("a/b~c") == "a~1b~0c"
    assert split("/field_meta/~1capacity~1it_mw") == ["field_meta", "/capacity/it_mw"]
    doc: Any = {"a": [{"b": 1}], "c/d": 2}
    assert resolve(doc, "/a/0/b") == (True, 1)
    assert resolve(doc, "/c~1d") == (True, 2)
    assert resolve(doc, "/a/1") == (False, None)
    assert resolve(doc, "/a/01") == (False, None)
    assert resolve(doc, "/x") == (False, None)
    with pytest.raises(ValueError, match="JSON Pointer"):
        split("a/b")


def test_claim_pointers_skip_nulls_defaults_empties_and_exempt(make_record: MakeRecord) -> None:
    r = make_record(
        capacity={"it_mw": 50},
        location={**make_record().location.model_dump(), "parcel_apns": ["1-2"]},
    )
    pointers = claim_pointers(r)
    assert "/capacity/it_mw" in pointers
    assert "/location/parcel_apns/0" in pointers
    assert "/capacity/facility_mw" not in pointers  # null
    assert "/grid/iso_rto" not in pointers  # default "unknown"
    assert "/purpose" not in pointers  # default "unknown"
    assert "/aliases" not in pointers  # empty list
    assert "/status_history/0/planned" not in pointers  # default False
    assert "/status_history/0/seq" in pointers
    for p in pointers:
        assert not any(p == e or p.startswith(e + "/") for e in EXEMPT_PREFIXES)
    assert not any(p.startswith("/sources") for p in pointers)
    assert "/location/precision" not in pointers


def test_status_prefix_does_not_exempt_status_history(make_record: MakeRecord) -> None:
    assert any(p.startswith("/status_history/") for p in claim_pointers(make_record()))


def test_rule_a_supports_entries_cover_exact_and_nested(make_record: MakeRecord) -> None:
    r = make_record(capacity={"it_mw": 50})
    assert is_supported(r, "/capacity/it_mw")  # "/capacity" covers it
    assert is_supported(r, "/canonical_name")
    assert not is_supported(r, "/money/investment_usd")


def test_rule_a_respects_token_boundaries(make_record: MakeRecord) -> None:
    base = make_record()
    sources = [s.model_copy(update={"supports": ["/loc"]}) for s in base.sources]
    r = base.model_copy(update={"sources": sources})
    assert not is_supported(r, "/location/lat")


def test_rule_b_items_with_their_own_source_ids(make_record: MakeRecord) -> None:
    r = make_record(
        aliases=[
            {"name": "Codename", "kind": "codename", "source_ids": ["s1"]},
            {"name": "Other", "kind": "press_name"},
        ],
        parties={"developer": [{"name": "Dev LLC", "source_ids": ["s1"]}, {"name": "No Source"}]},
    )
    r = r.model_copy(
        update={
            "sources": [
                r.sources[0].model_copy(update={"supports": ["/canonical_name", "/location"]})
            ]
        }
    )
    assert is_supported(r, "/aliases/0/name")
    assert not is_supported(r, "/aliases/1/name")
    assert is_supported(r, "/parties/developer/0/name")
    assert not is_supported(r, "/parties/developer/1/name")
    assert is_supported(r, "/status_history/0/event")
    assert unsupported_pointers(r) == [
        "/aliases/1/name",
        "/aliases/1/kind",
        "/parties/developer/1/name",
    ]


def test_rule_c_field_meta_derived_or_sourced(make_record: MakeRecord) -> None:
    r = make_record(
        grid={"iso_rto": "PJM", "utility_name": "Example Power"},
        money={"investment_usd": 1e9},
        field_meta={
            "/grid/iso_rto": {"confidence": 0.9, "method": "derived"},
            "/grid/utility_name": {"confidence": 0.7, "method": "inferred"},
            "/money": {"confidence": 0.8, "method": "stated", "source_ids": ["s1"]},
        },
    )
    assert is_supported(r, "/grid/iso_rto")
    assert not is_supported(r, "/grid/utility_name")  # inferred without source_ids
    assert is_supported(r, "/money/investment_usd")  # a prefix key with source_ids


def test_empty_supports_entry_supports_nothing(make_record: MakeRecord) -> None:
    base = make_record(money={"investment_usd": 1e9})
    r = base.model_copy(update={"sources": [base.sources[0].model_copy(update={"supports": [""]})]})
    assert not is_supported(r, "/money/investment_usd")
