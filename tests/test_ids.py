from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from atlas.ids import CROCKFORD, deterministic_ids, new_org_id, new_record_id
from atlas.schema.record import ORG_ID_PATTERN, PLACEHOLDER_ID, RECORD_ID_PATTERN


def test_crockford_alphabet_is_sorted_and_excludes_ambiguous_letters() -> None:
    assert list(CROCKFORD) == sorted(CROCKFORD)
    assert len(set(CROCKFORD)) == 32
    assert not set("ilou") & set(CROCKFORD)


def test_new_ids_match_the_patterns() -> None:
    for _ in range(50):
        assert re.match(RECORD_ID_PATTERN, new_record_id())
        assert re.match(ORG_ID_PATTERN, new_org_id())
    assert re.match(RECORD_ID_PATTERN, PLACEHOLDER_ID)


def test_injected_clock_and_rng_give_a_fixed_id() -> None:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    first = new_record_id(now, rand=lambda n: bytes(n))
    assert first == new_record_id(now, rand=lambda n: bytes(n))
    assert first.endswith("0" * 16)  # 80 zero bits
    assert new_record_id(now, rand=lambda n: b"\xff" * n) > first


def test_ids_sort_by_time() -> None:
    early = new_record_id(datetime(2026, 1, 1, tzinfo=UTC))
    late = new_record_id(datetime(2026, 1, 1, 0, 0, 0, 1000, tzinfo=UTC))
    assert early < late


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        new_record_id(datetime(2026, 1, 1))


def test_deterministic_ids_are_reproducible_unique_and_increasing() -> None:
    start = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    a, b = deterministic_ids(1, start), deterministic_ids(1, start)
    first = [a() for _ in range(200)]
    assert first == [b() for _ in range(200)]
    assert first == sorted(first)
    assert len(set(first)) == len(first)
    assert all(re.match(RECORD_ID_PATTERN, i) for i in first)
    other = deterministic_ids(2, start)
    assert other() != first[0]
    org = deterministic_ids(1, start, prefix="gwo-")()
    assert re.match(ORG_ID_PATTERN, org)
