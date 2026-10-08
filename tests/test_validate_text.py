"""Rule 7 (privacy) on the published form, the text rule and one-line issue output."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from atlas.jsonio import record_json
from atlas.publish import public_record
from atlas.schema.record import FacilityRecord
from atlas.text import find_personal_data
from atlas.validate import Issue, validate_record

MakeRecord = Callable[..., FacilityRecord]
TODAY = date(2026, 10, 12)
TAGS = "".join(chr(0xE0000 + ord(c)) for c in " tag text")


def issues_of(record: FacilityRecord) -> list[tuple[str, str | None]]:
    return [(i.rule, i.pointer) for i in validate_record(record, counties=None, today=TODAY)]


def with_note(make_record: MakeRecord, note: str) -> FacilityRecord:
    history = record_json(make_record())["status_history"]
    assert isinstance(history, list) and isinstance(history[0], dict)
    history[0]["note"] = note
    return make_record(status_history=history)


@pytest.mark.parametrize(
    "note",
    [
        "Contact jane.doe\u200b@example.com",  # zero-width space
        "Contact jane.doe@\u2060example.com",  # word joiner
        "Contact jane.doe@exa" + TAGS + "mple.com",  # tag characters
        "Call 571-555\u200b-0100",
        "Call 571-555\ufeff-0100",  # zero-width no-break space
        "Call 571\u00ad-555-0100",  # soft hyphen
    ],
)
def test_privacy_sees_through_invisible_characters(make_record: MakeRecord, note: str) -> None:
    """SV-1: publish strips invisible characters, so rule 7 matches the stripped text."""
    r = with_note(make_record, note)
    assert issues_of(r) == [("privacy", "/status_history/0/note")]
    # What publish would emit is exactly what rule 7 objected to.
    published = public_record(r).status_history[0].note
    assert published is not None and find_personal_data(published)


@pytest.mark.parametrize(
    "note", ["Contact jane.doe\uff20example.com", "Call \uff15\uff17\uff11-555-0100"]
)
def test_privacy_matches_the_nfkc_form(make_record: MakeRecord, note: str) -> None:
    assert issues_of(with_note(make_record, note)) == [("privacy", "/status_history/0/note")]


def test_phone_check_skips_identifier_fields(make_record: MakeRecord) -> None:
    """SV-11: a Cook County PIN or a dashed APN is not a phone number; emails are still found."""
    base = record_json(make_record())
    loc, sources = base["location"], base["sources"]
    assert isinstance(loc, dict) and isinstance(sources, list) and isinstance(sources[0], dict)
    loc["parcel_apns"] = ["08-35-302-012-0000", "123-456-7890"]
    sources[0]["supports"] = ["/canonical_name", "/location", "/grid", "/buildings"]
    r = make_record(
        location=loc,
        sources=sources,
        grid={"queue_ids": ["AF2-123-456-7890"]},
        external_ids={"tdlr_tabs": ["512-555-0142"]},
        buildings=[{"ref": "osm:way/512-555-0142"}],
    )
    assert issues_of(r) == []
    r = make_record(external_ids={"tdlr_tabs": ["jane@example.com"]})
    assert issues_of(r) == [("privacy", "/external_ids/tdlr_tabs/0")]
    r = make_record(canonical_name="Campus 512-555-0142 (Ashburn, VA)")
    assert issues_of(r) == [("privacy", "/canonical_name")]


@pytest.mark.parametrize(
    ("text", "codepoint"),
    [
        ("Data\ncenter", "U+000A"),
        ("Data\rcenter", "U+000D"),
        ("Data\tcenter", "U+0009"),
        ("Data center\x00", "U+0000"),
        ("Data center\x85", "U+0085"),
        ("Data center\u2028", "U+2028"),
        ("Data center\u2029", "U+2029"),
    ],
)
def test_text_rule_rejects_control_characters(
    make_record: MakeRecord, text: str, codepoint: str
) -> None:
    for r, pointer in (
        (make_record(canonical_name=text), "/canonical_name"),
        (with_note(make_record, text), "/status_history/0/note"),
    ):
        issues = validate_record(r, counties=None, today=TODAY)
        assert [(i.rule, i.pointer) for i in issues] == [("text", pointer)]
        assert codepoint in issues[0].message


@pytest.mark.parametrize("hidden", [TAGS, "\u202e", "\u00ad", "\ufe0f", "\u3164", "\u200b"])
def test_hidden_characters_in_display_text_are_published_without_them(
    make_record: MakeRecord, hidden: str
) -> None:
    """A8: publish strips them (and so do the importers); rule 7 checks the stripped form."""
    r = make_record(canonical_name=f"Lumen Ashburn{hidden} (Ashburn, VA)")
    assert issues_of(r) == []
    assert public_record(r).canonical_name == "Lumen Ashburn (Ashburn, VA)"


def test_hidden_characters_in_identifiers_are_rejected(make_record: MakeRecord) -> None:
    """Publish would strip them and so change the identifier."""
    phases = [{"phase_id": "p1\u200b", "name": "Phase 1", "source_ids": ["s1"]}]
    history: list[dict[str, Any]] = [
        {
            "seq": 1,
            "status": "announced",
            "event": "announced",
            "as_of": {"value": "2026-03", "precision": "month"},
            "phase_id": "p1\u200b",
            "source_ids": ["s1"],
        }
    ]
    r = make_record(phases=phases, status_history=history, external_ids={"osm": ["way/1" + TAGS]})
    assert sorted(issues_of(r), key=str) == [
        ("text", "/external_ids/osm/0"),
        ("text", "/phases/0/phase_id"),
        ("text", "/status_history/0/phase_id"),
    ]


def test_text_rule_checks_dict_keys(make_record: MakeRecord) -> None:
    r = make_record(external_ids={"osm\u200b": ["way/1"], "x\ny": ["1"]})
    assert ("text", "/external_ids/osm\u200b") in issues_of(r)
    assert ("text", "/external_ids/x\ny") in issues_of(r)


def test_text_rule_allows_ordinary_unicode(make_record: MakeRecord) -> None:
    r = make_record(canonical_name="Centro de Datos Querétaro — Straße 5 (Ashburn, VA)")
    assert issues_of(r) == []


@pytest.mark.parametrize(
    "value",
    [
        "p1\n::error file=SECURITY.md,line=1::Forged annotation",
        "p1\r::stop-commands::x",
        "p1\u2028::warning::x",
        "p1\x85::notice::x",
    ],
)
def test_issue_format_is_one_line(value: str) -> None:
    """SV-12: record strings cannot start a GitHub Actions workflow command line."""
    line = Issue("phase", f"phase_id {value} is not in phases", "gwa-x", "f.json", "/p").format()
    assert len(line.splitlines()) == 1
    assert line.startswith("f.json:/p [phase] phase_id p1\\u")


def test_issue_format_escapes_a_leading_workflow_command() -> None:
    line = Issue("file", "x", None, "::error::forged", None).format()
    assert line == "\\u003a:error::forged: [file] x"


def test_issue_format_keeps_printable_text() -> None:
    issue = Issue("geo", "point (39.0, -77.4) is not inside county 51107, Loudoun", "id", None)
    assert issue.format() == "id: [geo] point (39.0, -77.4) is not inside county 51107, Loudoun"
    assert Issue("geo", "Querétaro — x", "id").format() == "id: [geo] Querétaro — x"


def test_phase_id_injection_through_validate_record(make_record: MakeRecord) -> None:
    history: list[dict[str, Any]] = [
        {
            "seq": 1,
            "status": "announced",
            "event": "announced",
            "as_of": {"value": "2026-03", "precision": "month"},
            "phase_id": "p1\n::error::forged",
            "source_ids": ["s1"],
        }
    ]
    issues = validate_record(make_record(status_history=history), counties=None, today=TODAY)
    assert {i.rule for i in issues} == {"phase", "text"}
    for issue in issues:
        assert len(issue.format().splitlines()) == 1
    (phase,) = [i for i in issues if i.rule == "phase"]
    assert phase.format().endswith("phase_id p1\\u000a::error::forged is not in phases")
