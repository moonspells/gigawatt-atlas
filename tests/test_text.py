from __future__ import annotations

import pytest

from atlas.text import (
    EMAIL_RE,
    PHONE_RE,
    find_personal_data,
    normalize_name,
    normalize_org,
    strip_invisible,
)


@pytest.mark.parametrize(
    "text",
    [
        "TABS2026012091",
        "ZIP 78245",
        "17420 Omicron Dr., San Antonio, TX 78245",
        "96MW of data modules, $448,000,000",
        "2026-10-07",
        "gwa-01m47854008j5vt37xkj5ag72d",
    ],
)
def test_no_false_positives(text: str) -> None:
    assert find_personal_data(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "(512) 555-0142",
        "512-555-0142",
        "512.555.0142",
        "+1 512 555 0142",
        "call 1-512-555-0142 today",
    ],
)
def test_phone_numbers(text: str) -> None:
    assert PHONE_RE.search(text)
    assert len(find_personal_data(text)) == 1


def test_email_addresses() -> None:
    assert EMAIL_RE.search("permits@example.com")
    assert find_personal_data("write to a.b+c@sub.example.org or 512-555-0142") == [
        "a.b+c@sub.example.org",
        "512-555-0142",
    ]


def test_strip_invisible() -> None:
    hidden = "Data\u200b cen\u202eter\u2066\ufeff"
    assert strip_invisible(hidden) == "Data center"
    for ch in "\u200b\u200c\u200d\u2060\ufeff\u200e\u200f\u061c\u202a\u202e\u2066\u2069":
        assert strip_invisible(f"a{ch}b") == "ab"


def test_normalize_name() -> None:
    assert normalize_name("  \uff36antage  Data-Centers, TX12! ") == "vantage data centers tx12"
    assert normalize_name("Straße_Campus") == "strasse campus"


def test_normalize_org_drops_legal_forms() -> None:
    assert normalize_org("Vantage Data Centers TX1 LLC") == "vantage data centers tx1"
    assert normalize_org("Example Holdings, L.L.C.") == "example"
    assert normalize_org("Acme Corp.") == "acme"
    assert normalize_org("Company") == "company"
